"""StockSophist Charts - Backtest engine (SIMULATION ONLY).

Pure Python: no DOM, no Flask, no live-trading imports. Nothing in this
module can place a live order — it never imports mt5trade, trade_taker,
or any order-sending path, and it takes no broker connection. It replays
OHLC bars against dated BUY/SELL signal events and produces a trade
ledger, metrics, and an equity curve.

FILL CONTRACT (locked; see docs/BACKTEST.md for the plain-words version):
  * A signal confirmed at bar i's close fills at bar i+1's OPEN
    (default, conservative). The optimistic same-close fill exists only
    as a labelled option for signal-driven exits.
  * A bracket (SL/TP/trailing) created by a fill is live only from the
    bar AFTER the fill bar. Stops are never active before they exist.
  * Inside one bar, exits fire in this order: stop legs (fixed SL,
    trailing, breakeven) -> TP legs -> opposite-signal -> time/close
    exits. If a bar touches both a stop and a target, the STOP wins
    (SL-first: conservative on OHLC data, where the true intrabar path
    is unknowable).
  * Gap through a stop/limit: fill at that bar's OPEN, never at the
    untouched level. A stop-side gap fill is recorded as reason
    "GapExit"; a TP-side gap fill keeps its TP reason with gap=True.
  * Commission is charged on EVERY fill (including partial legs).
    Slippage is adverse on market/stop fills only — never on TP limit
    fills (a limit fills at limit-or-better) and never on top of a gap
    fill (the gap itself is the adverse move).
  * The forming/last incomplete bar must be excluded by the CALLER
    (the server route drops it); the engine trusts its bars are closed.
  * A position still open at the last bar is force-closed at the last
    close, reason "EndOfRange", flagged force_closed=True.
  * Margin/leverage is NOT modelled in v1: sizing is risk-based, not
    cash-constrained, and equity may go negative in theory (flagged as
    a warning, never hidden). A margin-call model is deferred, not faked.

TRAILING (all variants ratchet: a stop only ever moves favourably):
  * fixed:      stop = max(stop, peak_high - D)            (long)
  * pct:        stop = max(stop, peak_high * (1 - p))
  * atr:        stop = max(stop, peak_high - k * ATR(n))   (ATR recomputed
                per bar; expanding ATR must NEVER lower the stop)
  * chandelier: stop = max(stop, peak_high_last_N - k * ATR(n))
                (offered as atr with anchor="window")
  * breakeven:  on TP1 fill, stop = entry price (gap risk survives:
                never labelled "risk-free")
  * activation: fixed SL until close-confirmed profit >= activation,
                then the chosen trail takes over (close-based
                activation is the conservative, non-repainting choice)
  Trail levels update at each bar's CLOSE from that bar's data and are
  enforced on LATER bars' ranges (trigger="range", default) — or, with
  trigger="close", only closes beyond the level trigger the exit
  (reason "CloseExit").

C2C ("exit on close confirmation"): every signal-driven exit
(opposite-signal, time, flat-at-close) is recognised at a bar's close
and fills per c2c_timing: "next_open" (default, conservative) or
"same_close" (optimistic, labelled as such on the report). Bracket
fills (stop/TP prices) are NOT governed by c2c_timing — they fill at
their level (or at the open on gaps), as above.

SIGNAL ADAPTER: this codebase has NO discrete BUY/SELL event stream
(built-in indicators are reference overlays that "never signal or
alert"; the Alert Engine fires on text sweeps, not typed signals).
So the adapter is a signal-function interface: a provider is any
callable `events(bars, settings) -> [{bar_index, time, side}]` plus
metadata (warmup_bars, needs_ticks, htf). The v1 reference provider is
"supertrend_flip": BUY when the Supertrend direction flips to up,
SELL when it flips to down (closed-bar-only, causal — see
indicators/supertrend.py). Any other indicator plugs in through the
same interface and must pass the repaint gate (docs/BACKTEST.md).

MONEY MATH (XAUUSD convention, same as Trade Taker's fallback):
  point = 0.01, tick_value = $1.00 per lot. Value of a $1.00 price move
  per 1.0 lot = tick_value / point = $100 (100 oz). P&L of a leg =
  direction * (exit - entry) * qty_lots * 100. Risk value of 0.01 lot
  per $1.00 of SL distance = $1.00. MT5 symbol info overrides these
  when the caller supplies it (the server route does not in v1 — it
  passes the fallback, documented in docs/BACKTEST.md).
"""

import json
import math
import random

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

EPS = 1e-9


def _floor_step(x, step=0.01):
    """Floor x to a multiple of step (never rounds up — sizing rule)."""
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


def _atr_series(h, l, c, length):
    """Wilder ATR, same convention as indicators/supertrend.py and atr.py:
    first ATR = SMA of TR[1..length], defined at index `length`."""
    n = len(h)
    out = [None] * n
    if n <= length:
        return out
    tr = []
    for i in range(n):
        if i == 0:
            tr.append(h[0] - l[0])
        else:
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]),
                          abs(l[i] - c[i - 1])))
    out[length] = sum(tr[1:length + 1]) / length
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


# ---------------------------------------------------------------------------
# Signal providers
# ---------------------------------------------------------------------------

def supertrend_flip_events(bars, settings=None):
    """Reference provider: Supertrend direction flips -> dated events.

    BUY when trend flips to up, SELL when it flips to down. Event bar =
    the bar whose close confirms the flip (trend[i] != trend[i-1]).
    Causal: trend[i] uses only bars <= i (see compute_supertrend_series).
    Warm-up = ATR length (no events before the first defined ATR bar).

    Returns (events, warmup_bars). events: [{bar_index, time, side}].
    """
    settings = settings or {}
    length = max(2, int(settings.get("length", 10)))
    mult = float(settings.get("mult", 3.0))
    try:
        from indicators.supertrend import compute_supertrend_series
    except Exception:
        return [], length
    h = [float(b["high"]) for b in bars]
    l = [float(b["low"]) for b in bars]
    c = [float(b["close"]) for b in bars]
    _st, trend = compute_supertrend_series(h, l, c, length, mult)
    events = []
    # trend[] defaults to True before the first ATR-defined bar (index
    # `length`), so the first REAL state at index `length` must not emit
    # an event: a flip is only meaningful from index length+1 onward.
    for i in range(length + 1, len(bars)):
        if trend[i] != trend[i - 1]:
            events.append({"bar_index": i, "time": int(bars[i]["time"]),
                           "side": "BUY" if trend[i] else "SELL"})
    return events, length


PROVIDERS = {
    "supertrend_flip": {
        "name": "Supertrend flip (reference)",
        "fn": supertrend_flip_events,
        "needs_ticks": False,
        "htf": None,
        "settings_schema": [
            {"key": "length", "label": "ATR length", "type": "int",
             "default": 10},
            {"key": "mult", "label": "Multiplier", "type": "float",
             "default": 3.0},
        ],
    },
}


def repaint_gate(events_fn, bars, sample_every=None):
    """Full-history vs truncated-history gate (mechanical, not review).

    Runs events_fn on the full bars and on bars truncated at sampled
    bar indices k; events with bar_index <= k must be identical.
    Any difference = lookahead/repaint = FAIL. Returns
    {passed, checked, first_failing_bar}.
    """
    n = len(bars)
    if n == 0:
        return {"passed": True, "checked": [], "first_failing_bar": None}
    full, _w = events_fn(bars)
    full_map = {e["bar_index"]: e["side"] for e in full}
    if sample_every is None:
        sample_every = max(1, n // 12)
    checked = []
    for k in range(sample_every, n, sample_every):
        if k >= n - 1:
            continue
        trunc, _w2 = events_fn(bars[:k + 1])
        trunc_map = {e["bar_index"]: e["side"] for e in trunc}
        checked.append(k)
        for bi, side in trunc_map.items():
            if full_map.get(bi) != side:
                return {"passed": False, "checked": checked,
                        "first_failing_bar": bi}
        for bi, side in full_map.items():
            if bi <= k and trunc_map.get(bi) != side:
                return {"passed": False, "checked": checked,
                        "first_failing_bar": bi}
    return {"passed": True, "checked": checked, "first_failing_bar": None}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "symbol": "XAUUSD",
    "tf": "1m",
    "bar_seconds": 60,
    "initial_capital": 10000.0,
    "direction": "both",            # long | short | both
    "sizing": {"mode": "risk", "fixed_qty": 0.01, "risk_usd": 100.0,
               "risk_pct": None},
    "sl": {"mode": "price", "value": 5.0, "atr_length": 14, "atr_mult": 2.0},
    "tp_legs": [{"rr": 3.0, "fraction": 0.75}],
    "playbook": False,               # 75% at RR 3.0 + runner (see below)
    "trailing": {"type": "none", "distance": 5.0, "distance_unit": "price",
                 "atr_length": 14, "atr_mult": 2.0,
                 "activation": None, "activation_unit": "R",
                 "anchor": "entry", "anchor_bars": 22, "trigger": "range"},
    "breakeven_after_tp1": False,
    "c2c_timing": "next_open",     # next_open | same_close (optimistic)
    "opposite_exit": "close",       # off | close | reverse
    "max_bars": None,
    "close_exit_bars": None,
    "commission": {"type": "per_order", "value": 0.50},
    "slippage": 0.10,                # price units, adverse, market/stop only
    "point": 0.01,
    "tick_value": 1.0,               # $ per lot per tick (point)
    "warmup_bars": 0,
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_config(cfg_in):
    """Merge over defaults and validate. Raises ValueError on bad input."""
    cfg = _deep_merge(DEFAULT_CONFIG, cfg_in or {})
    if cfg["direction"] not in ("long", "short", "both"):
        raise ValueError("direction must be long, short, or both")
    if cfg["sizing"]["mode"] not in ("fixed", "risk"):
        raise ValueError("sizing.mode must be fixed or risk")
    if cfg["sizing"]["mode"] == "fixed" and cfg["sizing"]["fixed_qty"] <= 0:
        raise ValueError("fixed_qty must be > 0")
    if cfg["sizing"]["mode"] == "risk":
        if cfg["sizing"]["risk_usd"] is not None and cfg["sizing"]["risk_usd"] <= 0:
            raise ValueError("risk_usd must be > 0")
        if cfg["sizing"]["risk_pct"] is not None and not (0 < cfg["sizing"]["risk_pct"] <= 100):
            raise ValueError("risk_pct must be in (0, 100]")
        if cfg["sizing"]["risk_usd"] is None and cfg["sizing"]["risk_pct"] is None:
            raise ValueError("risk mode needs risk_usd or risk_pct")
    if cfg["sl"]["mode"] not in ("price", "atr"):
        raise ValueError("sl.mode must be price or atr")
    if cfg["sl"]["mode"] == "price" and cfg["sl"]["value"] <= 0:
        raise ValueError("sl.value (price distance) must be > 0")
    if cfg["sl"]["mode"] == "atr" and cfg["sl"]["atr_mult"] <= 0:
        raise ValueError("sl.atr_mult must be > 0")
    legs = cfg["tp_legs"] or []
    tot = sum(float(x.get("fraction", 0)) for x in legs)
    if tot > 1.0 + EPS:
        raise ValueError("tp_legs fractions must sum to <= 1.0")
    for x in legs:
        if x.get("rr", 0) <= 0:
            raise ValueError("tp leg rr must be > 0")
        if not (0 < x.get("fraction", 0) <= 1):
            raise ValueError("tp leg fraction must be in (0, 1]")
    if cfg["playbook"]:
        cfg["tp_legs"] = [{"rr": 3.0, "fraction": 0.75}]
    tr = cfg["trailing"]
    if tr["type"] not in ("none", "fixed", "pct", "atr"):
        raise ValueError("trailing.type must be none, fixed, pct, or atr")
    if tr["type"] == "fixed" and tr["distance"] <= 0:
        raise ValueError("trailing.distance must be > 0")
    if tr["type"] == "pct" and not (0 < tr["distance"] < 100):
        raise ValueError("trailing.distance (pct) must be in (0, 100)")
    if tr["type"] == "atr" and tr["atr_mult"] <= 0:
        raise ValueError("trailing.atr_mult must be > 0")
    if tr["trigger"] not in ("range", "close"):
        raise ValueError("trailing.trigger must be range or close")
    if cfg["c2c_timing"] not in ("next_open", "same_close"):
        raise ValueError("c2c_timing must be next_open or same_close")
    if cfg["opposite_exit"] not in ("off", "close", "reverse"):
        raise ValueError("opposite_exit must be off, close, or reverse")
    if cfg["commission"]["type"] not in ("percent", "per_order", "per_unit"):
        raise ValueError("commission.type must be percent, per_order, or per_unit")
    if cfg["commission"]["value"] < 0 or cfg["slippage"] < 0:
        raise ValueError("commission/slippage must be >= 0")
    if cfg["initial_capital"] <= 0:
        raise ValueError("initial_capital must be > 0")
    if cfg["point"] <= 0 or cfg["tick_value"] < 0:
        raise ValueError("point must be > 0, tick_value >= 0")
    return cfg


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _oz_per_lot(cfg):
    """Ounces (contract size) per lot implied by point/tick_value."""
    return cfg["tick_value"] / cfg["point"]


def _pnl(direction, entry, exit_price, qty, cfg):
    oz = _oz_per_lot(cfg)
    d = 1.0 if direction == "long" else -1.0
    return d * (exit_price - entry) * qty * oz


def _commission(cfg, qty, fill_price):
    c = cfg["commission"]
    if c["type"] == "percent":
        notional = qty * _oz_per_lot(cfg) * fill_price
        return notional * float(c["value"]) / 100.0
    if c["type"] == "per_order":
        return float(c["value"])
    return float(c["value"]) * qty  # per_unit: $ per lot


def _sl_price(cfg, fill_price, side, atr_value):
    sl = cfg["sl"]
    if sl["mode"] == "atr":
        if atr_value is None:
            return None
        dist = float(sl["atr_mult"]) * atr_value
    else:
        dist = float(sl["value"])
    if dist <= 0:
        return None
    return fill_price - dist if side == "long" else fill_price + dist


def _trail_level(cfg, pos, bar, atr_value):
    """Trail level from the position's peak, or None if trail inactive.

    Peak/ATR are as of the PREVIOUS bar's close (the caller updates the
    peak at each close); this function only evaluates the formula.
    Returns (level, kind) where kind is 'trail' — the caller decides
    the exit reason (TrailingStop vs CloseExit by trigger mode).
    """
    tr = cfg["trailing"]
    if tr["type"] == "none" or pos is None:
        return None
    if tr.get("activation") is not None and not pos.get("activated"):
        return None
    peak = pos.get("peak_high") if pos["side"] == "long" else pos.get("peak_low")
    if peak is None:
        return None
    if tr["type"] == "fixed":
        d = float(tr["distance"])
        return peak - d if pos["side"] == "long" else peak + d
    if tr["type"] == "pct":
        p = float(tr["distance"]) / 100.0
        return peak * (1.0 - p) if pos["side"] == "long" else peak * (1.0 + p)
    if tr["type"] == "atr":
        if atr_value is None:
            return None
        d = float(tr["atr_mult"]) * atr_value
        return peak - d if pos["side"] == "long" else peak + d
    return None


def _close_position(pos, fill_bar, fill_time, fill_price, reason, gap,
                    cfg, realized_acc):
    """Close the whole remaining position. Returns (parent_trade, net)."""
    qty = pos["qty_open"]
    gross = _pnl(pos["side"], pos["entry_price"], fill_price, qty, cfg)
    comm = _commission(cfg, qty, fill_price)
    net = gross - comm
    leg = {"parent": pos["index"], "leg": len(pos["legs"]),
           "reason": reason, "time": fill_time, "bar": fill_bar,
           "price": fill_price, "qty": qty, "gross": gross,
           "commission": comm, "net": net, "gap": bool(gap),
           "fraction": None}
    pos["legs"].append(leg)
    pos["qty_open"] = 0.0
    return leg


def _fill_leg(pos, leg_def, fill_bar, fill_time, fill_price, reason, gap,
              cfg):
    """Fill one TP leg (partial). Returns the leg row."""
    qty = min(leg_def["qty"], pos["qty_open"])
    if qty <= EPS:
        return None
    gross = _pnl(pos["side"], pos["entry_price"], fill_price, qty, cfg)
    comm = _commission(cfg, qty, fill_price)
    net = gross - comm
    leg = {"parent": pos["index"], "leg": len(pos["legs"]),
           "reason": reason, "time": fill_time, "bar": fill_bar,
           "price": fill_price, "qty": qty, "gross": gross,
           "commission": comm, "net": net, "gap": bool(gap),
           "fraction": leg_def["fraction"], "rr": leg_def["rr"]}
    pos["legs"].append(leg)
    pos["qty_open"] -= qty
    leg_def["filled"] = True
    return leg


def run_backtest(bars, events, cfg_in, provider_name="supertrend_flip"):
    """Run the engine over closed bars. Returns the full result dict.

    bars: [{time, open, high, low, close}] in ascending time order,
    closed bars only (caller excludes the forming bar).
    events: [{bar_index, time, side}] with side BUY|SELL, at most one
    per bar. Events with bar_index < warmup_bars are ignored.
    """
    cfg = validate_config(cfg_in)
    bars = [{"time": int(b["time"]), "open": float(b["open"]),
             "high": float(b["high"]), "low": float(b["low"]),
             "close": float(b["close"])} for b in (bars or [])]
    n = len(bars)
    empty = {"status": "ok", "symbol": cfg["symbol"], "tf": cfg["tf"],
             "bars": 0, "range": {"start": None, "end": None},
             "warmup_bars": cfg["warmup_bars"], "provider": provider_name,
             "metrics": {}, "splits": {}, "trades": [], "legs": [],
             "equity_curve": [], "buy_hold_curve": [],
             "exit_breakdown": {}, "snapshot": cfg, "assumptions": "",
             "warnings": {"skips": [], "unfilled_signals": 0,
                          "ignored_signals": 0,
                          "gap_fills": 0, "force_closes": 0,
                          "negative_equity": False}}
    if n == 0:
        empty["metrics"] = _empty_metrics()
        empty["splits"] = {"all": _empty_metrics(), "long": _empty_metrics(),
                           "short": _empty_metrics()}
        empty["assumptions"] = assumptions_line(cfg)
        return empty
    warmup = int(cfg["warmup_bars"] or 0)
    times = [b["time"] for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    atr_sl = None
    atr_tr = None
    if cfg["sl"]["mode"] == "atr":
        atr_sl = _atr_series(highs, lows, closes, int(cfg["sl"]["atr_length"]))
    if cfg["trailing"]["type"] == "atr":
        atr_tr = _atr_series(highs, lows, closes,
                             int(cfg["trailing"]["atr_length"]))
    events_by_bar = {}
    for e in (events or []):
        bi = int(e["bar_index"])
        if bi not in events_by_bar and e.get("side") in ("BUY", "SELL"):
            events_by_bar[bi] = e["side"]

    slip = float(cfg["slippage"])
    trades = []
    flat_legs = []
    equity_close = []
    equity_intra = []
    realized = 0.0
    warnings = {"skips": [], "unfilled_signals": 0, "ignored_signals": 0,
                "gap_fills": 0, "force_closes": 0,
                "negative_equity": False}
    pos = None
    pending_entry = None   # {side, signal_bar}
    pending_exit = None    # {reason, reverse_side or None}

    def direction_allowed(side):
        d = cfg["direction"]
        return d == "both" or (d == "long" and side == "long") or \
            (d == "short" and side == "short")

    def open_position(side, fill_bar, fill_price, equity_at_fill):
        atr_v = atr_sl[fill_bar] if atr_sl is not None else None
        sl_price = _sl_price(cfg, fill_price, side, atr_v)
        if sl_price is None:
            warnings["skips"].append(
                {"bar": fill_bar, "side": side,
                 "reason": "no ATR for SL at fill bar"})
            return None
        sl_dist = abs(fill_price - sl_price)
        if sl_dist <= EPS:
            warnings["skips"].append(
                {"bar": fill_bar, "side": side, "reason": "zero SL distance"})
            return None
        if cfg["sizing"]["mode"] == "risk":
            risk_value = cfg["sizing"]["risk_usd"]
            if risk_value is None and cfg["sizing"]["risk_pct"] is not None:
                risk_value = float(cfg["sizing"]["risk_pct"]) / 100.0 * equity_at_fill
            risk_per_lot = sl_dist / cfg["point"] * cfg["tick_value"]
            qty = _floor_step(float(risk_value) / risk_per_lot, 0.01)
            if qty < 0.01 - EPS:
                warnings["skips"].append(
                    {"bar": fill_bar, "side": side,
                     "reason": "risk too small for 0.01 lot (refused, never above risk)"})
                return None
        else:
            qty = float(cfg["sizing"]["fixed_qty"])
        planned_risk = qty * sl_dist / cfg["point"] * cfg["tick_value"]
        leg_defs = []
        for leg in cfg["tp_legs"]:
            lp = fill_price + (leg["rr"] * sl_dist if side == "long"
                               else -leg["rr"] * sl_dist)
            lq = _floor_step(qty * float(leg["fraction"]), 0.01)
            if lq >= 0.01 - EPS:
                leg_defs.append({"rr": float(leg["rr"]),
                                 "fraction": float(leg["fraction"]),
                                 "price": lp, "qty": lq, "filled": False})
        runner_qty = qty - sum(x["qty"] for x in leg_defs)
        if runner_qty < 0.01 - EPS:
            runner_qty = 0.0
        return {"index": len(trades), "side": side, "fill_bar": fill_bar,
                "fill_time": times[fill_bar], "entry_price": fill_price,
                "qty_initial": qty, "qty_open": qty,
                "sl_price": sl_price, "sl_dist": sl_dist,
                "planned_risk": planned_risk, "leg_defs": leg_defs,
                "runner_qty": runner_qty, "trail_level": None,
                "breakeven": False,
                "activated": cfg["trailing"].get("activation") is None,
                "peak_high": None, "peak_low": None,
                "mfe": 0.0, "mae": 0.0, "legs": []}

    def finalize_position(p, last_reason):
        gross = sum(x["gross"] for x in p["legs"])
        comm = sum(x["commission"] for x in p["legs"])
        net = gross - comm
        last_leg = p["legs"][-1] if p["legs"] else None
        trade = {"index": p["index"], "side": p["side"],
                 "entry_bar": p["fill_bar"], "entry_time": p["fill_time"],
                 "entry_price": p["entry_price"],
                 "qty_initial": p["qty_initial"],
                 "sl_price": p["sl_price"],
                 "sl_dist": p["sl_dist"],
                 "planned_risk": p["planned_risk"],
                 "gross": gross, "commission": comm, "net": net,
                 "r": (net / p["planned_risk"]) if p["planned_risk"] > EPS else 0.0,
                 "mfe": p["mfe"], "mae": p["mae"],
                 "mfe_r": (p["mfe"] / p["planned_risk"]) if p["planned_risk"] > EPS else 0.0,
                 "mae_r": (p["mae"] / p["planned_risk"]) if p["planned_risk"] > EPS else 0.0,
                 "duration_bars": (last_leg["bar"] - p["fill_bar"]) if last_leg else 0,
                 "exit_bar": last_leg["bar"] if last_leg else p["fill_bar"],
                 "exit_time": last_leg["time"] if last_leg else p["fill_time"],
                 "final_reason": last_reason,
                 "force_closed": last_reason == "EndOfRange",
                 "legs": p["legs"]}
        trades.append(trade)
        flat_legs.extend(p["legs"])
        return net

    for j in range(n):
        bar = bars[j]
        realized_before_bar = realized
        pos_open_at_bar_start = pos is not None and pos["qty_open"] > 0
        start_qty = pos["qty_open"] if pos_open_at_bar_start else 0.0
        start_side = pos["side"] if pos_open_at_bar_start else None
        start_entry = pos["entry_price"] if pos_open_at_bar_start else 0.0

        # (a) pending signal/time exit fills at this bar's open.
        if pending_exit is not None and pos is not None:
            px = opens[j] - slip if pos["side"] == "long" else opens[j] + slip
            leg = _close_position(pos, j, times[j], px,
                                  pending_exit["reason"], False, cfg, realized)
            realized += leg["net"]
            rev = pending_exit.get("reverse_side")
            finalize_position(pos, pending_exit["reason"])
            pos = None
            pending_exit = None
            if rev is not None:
                eq = realized + cfg["initial_capital"]
                pos = open_position(rev, j, px, eq)
                if pos is not None:
                    comm = _commission(cfg, pos["qty_initial"], px)
                    # Entry fill commission is charged on the entry leg
                    # itself at close time via the exit math: record it
                    # as an entry cost leg so totals stay exact.
                    pos["entry_commission"] = comm
                    realized -= comm
                    # The reverse entry's bracket is live from bar j+1.
                    pos_open_at_bar_start = False
                    start_qty = 0.0

        # (b) pending entry fills at this bar's open (adverse slippage).
        if pending_entry is not None and pos is None:
            side = pending_entry["side"]
            px = opens[j] + slip if side == "long" else opens[j] - slip
            eq = realized + cfg["initial_capital"]
            pos = open_position(side, j, px, eq)
            if pos is not None:
                comm = _commission(cfg, pos["qty_initial"], px)
                pos["entry_commission"] = comm
                realized -= comm
            pending_entry = None
            # Bracket is live only from bar j+1: skip exit checks below.

        # (c) bracket exits for a live position (bars after the fill bar).
        closed_this_bar = False
        if pos is not None and pos["qty_open"] > 0 and j > pos["fill_bar"]:
            side = pos["side"]
            is_long = side == "long"
            # --- stop legs (range-based), SL-first ---
            eff_stop = pos["sl_price"]
            stop_reason = "StopLoss"
            if pos["breakeven"]:
                be = pos["entry_price"]
                if (is_long and be > eff_stop) or (not is_long and be < eff_stop):
                    eff_stop = be
                    stop_reason = "BreakevenStop"
            tr_level = pos.get("trail_level")
            if tr_level is not None and cfg["trailing"]["trigger"] == "range":
                if (is_long and tr_level > eff_stop) or \
                        (not is_long and tr_level < eff_stop):
                    eff_stop = tr_level
                    stop_reason = "TrailingStop"
            stop_hit = False
            stop_gap = False
            if is_long:
                if opens[j] <= eff_stop + EPS:
                    stop_hit, stop_gap = True, True
                elif lows[j] <= eff_stop + EPS:
                    stop_hit = True
            else:
                if opens[j] >= eff_stop - EPS:
                    stop_hit, stop_gap = True, True
                elif highs[j] >= eff_stop - EPS:
                    stop_hit = True
            if stop_hit:
                if stop_gap:
                    px = opens[j]
                    reason = "GapExit"
                    warnings["gap_fills"] += 1
                else:
                    px = eff_stop - slip if is_long else eff_stop + slip
                    reason = stop_reason
                leg = _close_position(pos, j, times[j], px, reason,
                                      stop_gap, cfg, realized)
                realized += leg["net"]
                finalize_position(pos, reason)
                pos = None
                closed_this_bar = True
            else:
                # --- trailing close-trigger (reason CloseExit) ---
                if tr_level is not None and \
                        cfg["trailing"]["trigger"] == "close":
                    crossed = (closes[j] <= tr_level + EPS) if is_long \
                        else (closes[j] >= tr_level - EPS)
                    if crossed:
                        if cfg["c2c_timing"] == "same_close":
                            px = closes[j]
                            leg = _close_position(
                                pos, j, times[j], px, "CloseExit", False,
                                cfg, realized)
                            realized += leg["net"]
                            finalize_position(pos, "CloseExit")
                            pos = None
                            closed_this_bar = True
                        else:
                            pending_exit = {"reason": "CloseExit",
                                            "reverse_side": None}
                # --- TP legs (in order) ---
                if pos is not None and pos["qty_open"] > 0:
                    for leg_idx, leg_def in enumerate(list(pos["leg_defs"])):
                        if leg_def["filled"] or pos["qty_open"] <= EPS:
                            continue
                        tp = leg_def["price"]
                        hit = gap = False
                        if is_long:
                            if opens[j] >= tp - EPS:
                                hit, gap = True, True
                            elif highs[j] >= tp - EPS:
                                hit = True
                        else:
                            if opens[j] <= tp + EPS:
                                hit, gap = True, True
                            elif lows[j] <= tp + EPS:
                                hit = True
                        if not hit:
                            continue
                        px = opens[j] if gap else tp
                        if gap:
                            warnings["gap_fills"] += 1
                        leg = _fill_leg(pos, leg_def, j, times[j], px,
                                        "TakeProfit%d" % (leg_idx + 1),
                                        gap, cfg)
                        if leg is None:
                            continue
                        realized += leg["net"]
                        if leg_idx == 0 and cfg["breakeven_after_tp1"]:
                            pos["breakeven"] = True
                        if pos["qty_open"] <= EPS:
                            finalize_position(
                                pos, leg["reason"])
                            pos = None
                            closed_this_bar = True
                            break

        # (d) trail state update at this bar's close (bars after fill bar).
        if pos is not None and pos["qty_open"] > 0 and j > pos["fill_bar"]:
            is_long = pos["side"] == "long"
            if is_long:
                pos["peak_high"] = highs[j] if pos["peak_high"] is None \
                    else max(pos["peak_high"], highs[j])
            else:
                pos["peak_low"] = lows[j] if pos["peak_low"] is None \
                    else min(pos["peak_low"], lows[j])
            # activation (close-confirmed profit in R or price units)
            tr = cfg["trailing"]
            if not pos["activated"] and tr.get("activation") is not None:
                profit = (closes[j] - pos["entry_price"]) if is_long \
                    else (pos["entry_price"] - closes[j])
                if tr["activation_unit"] == "R":
                    need = float(tr["activation"]) * pos["sl_dist"]
                else:
                    need = float(tr["activation"])
                if profit >= need - EPS:
                    pos["activated"] = True
            # trail level from the peak (ratcheted: never moves back)
            atr_v = None
            if tr["type"] == "atr" and atr_tr is not None:
                atr_v = atr_tr[j]
            peak_for_trail = None
            if tr["anchor"] == "window" and tr["type"] == "atr":
                w = int(tr["anchor_bars"])
                if is_long:
                    peak_for_trail = max(highs[max(0, j - w + 1):j + 1])
                else:
                    peak_for_trail = min(lows[max(0, j - w + 1):j + 1])
            else:
                peak_for_trail = pos["peak_high"] if is_long else pos["peak_low"]
            saved_peak = (pos["peak_high"] if is_long else pos["peak_low"])
            if is_long:
                pos["peak_high"] = peak_for_trail
            else:
                pos["peak_low"] = peak_for_trail
            lvl = _trail_level(cfg, pos, bar, atr_v)
            if is_long:
                pos["peak_high"] = saved_peak
            else:
                pos["peak_low"] = saved_peak
            if lvl is not None:
                cur = pos.get("trail_level")
                if cur is None:
                    pos["trail_level"] = lvl
                elif is_long:
                    pos["trail_level"] = max(cur, lvl)
                else:
                    pos["trail_level"] = min(cur, lvl)

        # (e) signals at this bar's close -> pending entry/exit for j+1.
        if j < n - 1:
            ev_side = events_by_bar.get(j)
            if ev_side is not None and j >= warmup:
                want = "long" if ev_side == "BUY" else "short"
                if pos is not None and pos["qty_open"] > 0:
                    if want != pos["side"] and cfg["opposite_exit"] != "off":
                        if cfg["c2c_timing"] == "same_close":
                            px = closes[j]
                            leg = _close_position(
                                pos, j, times[j], px, "OppositeSignal",
                                False, cfg, realized)
                            realized += leg["net"]
                            if cfg["opposite_exit"] == "reverse":
                                finalize_position(pos, "OppositeSignal")
                                pos = None
                                eq = realized + cfg["initial_capital"]
                                pos = open_position(want, j, px, eq)
                                if pos is not None:
                                    comm = _commission(
                                        cfg, pos["qty_initial"], px)
                                    pos["entry_commission"] = comm
                                    realized -= comm
                            else:
                                finalize_position(pos, "OppositeSignal")
                                pos = None
                        elif pending_exit is None:
                            pending_exit = {
                                "reason": "OppositeSignal",
                                "reverse_side": want
                                if cfg["opposite_exit"] == "reverse" else None}
                    else:
                        # Same-side signal while in position, opposite
                        # with opposite_exit off, or an exit already
                        # pending: ignored, counted.
                        warnings["ignored_signals"] += 1
                elif pos is None and pending_entry is None and \
                        pending_exit is None and direction_allowed(want):
                    pending_entry = {"side": want, "signal_bar": j}
                else:
                    # Direction filter, or a pending entry/exit already
                    # exists: signal ignored, counted, not an error.
                    warnings["ignored_signals"] += 1
            # (f) time / flat-at-close exits decided at this bar's close.
            if pos is not None and pos["qty_open"] > 0 and \
                    j > pos["fill_bar"] and pending_exit is None:
                held = j - pos["fill_bar"]
                reason = None
                if cfg.get("close_exit_bars") is not None and \
                        held >= int(cfg["close_exit_bars"]):
                    reason = "CloseExit"
                elif cfg.get("max_bars") is not None and \
                        held >= int(cfg["max_bars"]):
                    reason = "TimeExit"
                if reason is not None:
                    if cfg["c2c_timing"] == "same_close":
                        px = closes[j]
                        leg = _close_position(pos, j, times[j], px, reason,
                                              False, cfg, realized)
                        realized += leg["net"]
                        finalize_position(pos, reason)
                        pos = None
                    else:
                        pending_exit = {"reason": reason,
                                        "reverse_side": None}

        # (g) MFE/MAE + mark-to-market equity at this bar's close.
        if pos is not None and pos["qty_open"] > 0:
            is_long = pos["side"] == "long"
            oz = _oz_per_lot(cfg)
            if is_long:
                pos["mfe"] = max(pos["mfe"],
                                 (highs[j] - pos["entry_price"]) * pos["qty_initial"] * oz)
                pos["mae"] = min(pos["mae"],
                                 (lows[j] - pos["entry_price"]) * pos["qty_initial"] * oz)
            else:
                pos["mfe"] = max(pos["mfe"],
                                 (pos["entry_price"] - lows[j]) * pos["qty_initial"] * oz)
                pos["mae"] = min(pos["mae"],
                                 (pos["entry_price"] - highs[j]) * pos["qty_initial"] * oz)
            pos["mfe"] = max(0.0, pos["mfe"])
            pos["mae"] = min(0.0, pos["mae"])
        eq_close = cfg["initial_capital"] + realized
        if pos is not None and pos["qty_open"] > 0:
            eq_close += _pnl(pos["side"], pos["entry_price"], closes[j],
                             pos["qty_open"], cfg)
        equity_close.append({"time": times[j], "equity": eq_close})
        # intrabar-low equity for the intrabar drawdown basis.
        intra = eq_close
        if pos_open_at_bar_start or (pos is not None and pos["qty_open"] > 0
                                     and pos["fill_bar"] == j):
            if start_side is not None:
                oz = _oz_per_lot(cfg)
                if start_side == "long":
                    worst = (lows[j] - start_entry) * start_qty * oz
                else:
                    worst = (start_entry - highs[j]) * start_qty * oz
                cand = cfg["initial_capital"] + realized_before_bar + worst
                intra = min(eq_close, cand)
            elif pos is not None and pos["fill_bar"] == j:
                oz = _oz_per_lot(cfg)
                if pos["side"] == "long":
                    worst = (lows[j] - pos["entry_price"]) * pos["qty_initial"] * oz
                else:
                    worst = (pos["entry_price"] - highs[j]) * pos["qty_initial"] * oz
                cand = cfg["initial_capital"] + realized_before_bar + worst
                intra = min(eq_close, cand)
        equity_intra.append({"time": times[j], "equity": intra})
        if eq_close < 0:
            warnings["negative_equity"] = True

    # Signals on the last bar have no next bar to fill into: unfilled.
    for bi in events_by_bar:
        if bi >= n - 1 and bi >= warmup:
            warnings["unfilled_signals"] += 1
    # End of range: force-close anything still open at the last close.
    if pos is not None and pos["qty_open"] > 0:
        leg = _close_position(pos, n - 1, times[-1], closes[-1],
                              "EndOfRange", False, cfg, realized)
        realized += leg["net"]
        warnings["force_closes"] += 1
        finalize_position(pos, "EndOfRange")
        pos = None
        # The last equity point was computed with MTM (no exit
        # commission); the position is now closed at that same close,
        # so the curve must end at realized equity exactly.
        final_eq = cfg["initial_capital"] + realized
        equity_close[-1]["equity"] = final_eq
        equity_intra[-1]["equity"] = min(equity_intra[-1]["equity"],
                                         final_eq)
    # Entry commissions were subtracted from `realized` at fill time (so
    # equity is exact) but are not inside leg commissions. Fold them into
    # each trade's commission/net and its first leg, so the ledger sums
    # to the equity curve exactly. See _finalize_entry_commissions.
    _finalize_entry_commissions(trades, bars, cfg)
    metrics, splits = _compute_metrics(trades, equity_close, equity_intra,
                                       cfg)
    breakdown = {}
    for leg in flat_legs:
        b = breakdown.setdefault(leg["reason"],
                                 {"count": 0, "net": 0.0})
        b["count"] += 1
        b["net"] += leg["net"]
    first_close = closes[0]
    bh_curve = [{"time": times[i],
                 "equity": cfg["initial_capital"] * closes[i] / first_close}
                for i in range(n)] if first_close else []
    if first_close and closes[-1]:
        metrics["buy_hold_return_pct"] = (closes[-1] / first_close - 1.0) * 100.0
    return {"status": "ok", "symbol": cfg["symbol"], "tf": cfg["tf"],
            "bars": n, "range": {"start": times[0], "end": times[-1]},
            "warmup_bars": warmup, "provider": provider_name,
            "metrics": metrics, "splits": splits, "trades": trades,
            "legs": flat_legs, "equity_curve": equity_close,
            "buy_hold_curve": bh_curve, "exit_breakdown": breakdown,
            "snapshot": cfg, "assumptions": assumptions_line(cfg),
            "warnings": warnings}


def _finalize_entry_commissions(trades, bars, cfg):
    """Fold per-trade entry commissions into trade totals.

    Entry commission was charged against realized equity at fill time
    (so equity/metrics are exact), but leg rows only carry exit-fill
    commissions. This pass adds the entry commission to each trade's
    commission and net (and to its first leg's commission, so the
    ledger sums to the equity curve). Entry commission per trade =
    commission(qty_initial, entry_price) — recomputed deterministically
    from the trade record, so no position state is needed.
    """
    for t in trades:
        ec = _commission(cfg, t["qty_initial"], t["entry_price"])
        t["commission"] += ec
        t["net"] -= ec
        pr = t.get("planned_risk", 0.0)
        t["r"] = (t["net"] / pr) if pr > EPS else 0.0
        if t["legs"]:
            t["legs"][0]["commission"] += ec
            t["legs"][0]["net"] -= ec


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _empty_metrics():
    return {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate": None, "net": 0.0, "gross_profit": 0.0,
            "gross_loss": 0.0, "profit_factor": None,
            "expectancy_usd": None, "expectancy_r": None,
            "max_dd_usd": 0.0, "max_dd_pct": 0.0, "dd_peak_time": None,
            "dd_trough_time": None, "underwater_bars": 0,
            "recovery_factor": None, "calmar": None, "cagr": None,
            "sharpe": None, "sortino": None,
            "max_win_streak": 0, "max_win_streak_usd": 0.0,
            "max_loss_streak": 0, "max_loss_streak_usd": 0.0,
            "avg_mfe": None, "avg_mae": None, "corr_net_mfe": None,
            "corr_net_mae": None, "buy_hold_return_pct": None}


def _trade_metrics(trades):
    """Trade-level metrics only (no equity path needed)."""
    m = _empty_metrics()
    m["trades"] = len(trades)
    if not trades:
        return m
    wins = [t for t in trades if t["net"] > EPS]
    losses = [t for t in trades if t["net"] < -EPS]
    be = [t for t in trades if abs(t["net"]) <= EPS]
    m["wins"], m["losses"], m["breakeven"] = len(wins), len(losses), len(be)
    m["win_rate"] = len(wins) / len(trades)
    m["net"] = sum(t["net"] for t in trades)
    m["gross_profit"] = sum(t["net"] for t in wins)
    m["gross_loss"] = sum(t["net"] for t in losses)
    m["profit_factor"] = (m["gross_profit"] / abs(m["gross_loss"])
                          if m["gross_loss"] < -EPS else None)
    m["expectancy_usd"] = m["net"] / len(trades)
    m["expectancy_r"] = sum(t["r"] for t in trades) / len(trades)
    m["avg_mfe"] = sum(t["mfe"] for t in trades) / len(trades)
    m["avg_mae"] = sum(t["mae"] for t in trades) / len(trades)
    m["corr_net_mfe"] = _pearson([t["net"] for t in trades],
                                 [t["mfe"] for t in trades])
    m["corr_net_mae"] = _pearson([t["net"] for t in trades],
                                 [t["mae"] for t in trades])
    ws = ls = 0
    ws_usd = ls_usd = 0.0
    for t in trades:
        if t["net"] > EPS:
            ws += 1
            ws_usd += t["net"]
            ls, ls_usd = 0, 0.0
            if ws > m["max_win_streak"]:
                m["max_win_streak"], m["max_win_streak_usd"] = ws, ws_usd
        elif t["net"] < -EPS:
            ls += 1
            ls_usd += t["net"]
            ws, ws_usd = 0, 0.0
            if ls > m["max_loss_streak"]:
                m["max_loss_streak"], m["max_loss_streak_usd"] = ls, ls_usd
        else:
            ws, ws_usd, ls, ls_usd = 0, 0.0, 0, 0.0
    return m


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= EPS or sy <= EPS:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sx * sy)


def _drawdown(series):
    """series: [{time, equity}]. Returns dd dict for one basis."""
    peak = None
    peak_time = None
    max_dd = 0.0
    max_pct = 0.0
    trough_time = None
    dd_peak_time = None
    peak_idx = 0
    max_uw = 0
    uw_start = 0
    for i, p in enumerate(series):
        eq = p["equity"]
        if peak is None or eq > peak:
            if peak is not None and max_dd > 0 and i - uw_start > max_uw:
                max_uw = i - uw_start
            peak, peak_time, peak_idx = eq, p["time"], i
            uw_start = i
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            trough_time = p["time"]
            dd_peak_time = peak_time
            if peak > EPS:
                max_pct = dd / peak
        if eq >= peak - EPS and i > uw_start:
            if i - uw_start > max_uw:
                max_uw = i - uw_start
            uw_start = i
    if series and max_dd > 0:
        # underwater at the end counts too
        if len(series) - 1 - uw_start > max_uw:
            max_uw = len(series) - 1 - uw_start
    return {"max_dd_usd": max_dd, "max_dd_pct": max_pct,
            "dd_peak_time": dd_peak_time, "dd_trough_time": trough_time,
            "underwater_bars": max_uw}


def _sharpe_sortino(equity_curve, bar_seconds):
    if len(equity_curve) < 3:
        return None, None
    rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity"]
        cur = equity_curve[i]["equity"]
        if prev > EPS:
            rets.append((cur - prev) / prev)
    if len(rets) < 3:
        return None, None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    dd_var = sum(min(0.0, r) ** 2 for r in rets) / len(rets)
    dd = math.sqrt(dd_var) if dd_var > 0 else 0.0
    bars_per_year = 260.0 * 24.0 * 3600.0 / max(1, bar_seconds)
    ann = math.sqrt(bars_per_year)
    sharpe = (mean / sd * ann) if sd > EPS else None
    sortino = (mean / dd * ann) if dd > EPS else None
    return sharpe, sortino


def _compute_metrics(trades, equity_close, equity_intra, cfg):
    m = _trade_metrics(trades)
    dd_intra = _drawdown(equity_intra)
    dd_ctc = _drawdown(equity_close)
    # Report the WORSE of the two bases as the headline, both kept.
    if dd_intra["max_dd_usd"] >= dd_ctc["max_dd_usd"]:
        dd = dd_intra
    else:
        dd = dd_ctc
    m.update(dd)
    m["dd_intrabar"] = dd_intra
    m["dd_close"] = dd_ctc
    init = cfg["initial_capital"]
    final = equity_close[-1]["equity"] if equity_close else init
    if dd["max_dd_usd"] > EPS:
        m["recovery_factor"] = m["net"] / dd["max_dd_usd"]
    if equity_close and len(equity_close) >= 2:
        days = (equity_close[-1]["time"] - equity_close[0]["time"]) / 86400.0
        # CAGR annualises over years; on sub-day spans the exponent
        # explodes (and the number is meaningless), so report None.
        if days >= 1.0 and init > 0 and final > 0:
            try:
                m["cagr"] = (final / init) ** (365.25 / days) - 1.0
            except OverflowError:
                m["cagr"] = None
            if m["cagr"] is not None and dd["max_dd_pct"] > EPS:
                m["calmar"] = m["cagr"] / dd["max_dd_pct"]
    sharpe, sortino = _sharpe_sortino(equity_close, cfg["bar_seconds"])
    m["sharpe"], m["sortino"] = sharpe, sortino
    if equity_close and equity_close[0]["equity"] > EPS:
        m["buy_hold_return_pct"] = None  # filled by caller with price data
    splits = {"all": m}
    for side in ("long", "short"):
        splits[side] = _trade_metrics([t for t in trades if t["side"] == side])
    return m, splits


def assumptions_line(cfg):
    parts = [
        "Fill: signal confirmed at bar close fills at NEXT bar open "
        "(%s)" % ("optimistic same-close for signal exits"
                  if cfg["c2c_timing"] == "same_close"
                  else "conservative next-open"),
        "Intrabar: SL-first if a bar touches stop and target; "
        "gap-through-stop fills at the open, never at the stop",
        "Costs: commission %s %s per fill, slippage %.2f price units "
        "adverse on market/stop fills only" % (
            cfg["commission"]["type"], cfg["commission"]["value"],
            cfg["slippage"]),
        "Trailing: %s, trigger %s, updated at bar closes (non-repainting)"
        % (cfg["trailing"]["type"], cfg["trailing"]["trigger"]),
        "Margin NOT modelled (risk sizing only); equity may go negative "
        "in theory and is flagged, never hidden",
        "Backtest is not a live-trading guarantee: OHLC intrabar paths "
        "are modelled, not observed",
    ]
    if cfg["commission"]["value"] == 0 and cfg["slippage"] == 0:
        parts.append("WARNING: zero costs — results are optimistic")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Robustness: walk-forward, sweep, Monte Carlo, cost stress
# ---------------------------------------------------------------------------

def walk_forward(bars, events, cfg_in, split_ratio=0.7):
    """In-sample / out-of-sample split view. Positions never span slices:
    each slice is run standalone (an open position at a slice end is
    force-closed inside that slice)."""
    cfg = validate_config(cfg_in)
    n = len(bars)
    k = max(1, int(n * split_ratio))
    if k >= n:
        k = n - 1
    ev_is = [e for e in events if int(e["bar_index"]) < k]
    ev_oos = [{"bar_index": int(e["bar_index"]) - k, "time": e["time"],
               "side": e["side"]} for e in events
              if int(e["bar_index"]) >= k]
    res_is = run_backtest(bars[:k], ev_is, cfg, provider_name="walk-forward")
    res_oos = run_backtest(bars[k:], ev_oos, cfg, provider_name="walk-forward")
    return {"split_ratio": split_ratio, "split_bar": k,
            "in_sample": {"metrics": res_is["metrics"],
                          "trades": res_is["trades"]},
            "out_of_sample": {"metrics": res_oos["metrics"],
                              "trades": res_oos["trades"]}}


def parameter_sweep(bars, events, cfg_in, factors=(0.8, 0.9, 1.1, 1.2)):
    """Vary SL, TP, and trail one at a time by +/-10-20%.

    Stability share = fraction of variants still profitable (net > 0).
    A plateau (high share) is robust; a sharp peak (low share) is not.
    """
    cfg = validate_config(cfg_in)
    base = run_backtest(bars, events, cfg)
    variants = []
    for param in ("sl", "tp", "trail"):
        for f in factors:
            import copy
            c = copy.deepcopy(cfg)
            if param == "sl":
                if c["sl"]["mode"] == "price":
                    c["sl"]["value"] = float(c["sl"]["value"]) * f
                else:
                    c["sl"]["atr_mult"] = float(c["sl"]["atr_mult"]) * f
            elif param == "tp":
                c["tp_legs"] = [{"rr": float(x["rr"]) * f,
                                 "fraction": x["fraction"]}
                                for x in c["tp_legs"]]
                if c["playbook"]:
                    c["playbook"] = False  # sweep the legs, not the preset
            else:
                if c["trailing"]["type"] == "none":
                    continue
                if c["trailing"]["type"] == "pct":
                    c["trailing"]["distance"] = float(
                        c["trailing"]["distance"]) * f
                elif c["trailing"]["type"] == "atr":
                    c["trailing"]["atr_mult"] = float(
                        c["trailing"]["atr_mult"]) * f
                else:
                    c["trailing"]["distance"] = float(
                        c["trailing"]["distance"]) * f
            r = run_backtest(bars, events, c)
            variants.append({"param": param, "factor": f,
                             "net": r["metrics"]["net"],
                             "win_rate": r["metrics"]["win_rate"],
                             "max_dd_usd": r["metrics"]["max_dd_usd"]})
    profitable = sum(1 for v in variants if v["net"] > 0)
    share = (profitable / len(variants)) if variants else None
    return {"base_net": base["metrics"]["net"],
            "base_win_rate": base["metrics"]["win_rate"],
            "variants": variants, "stability_share": share,
            "profitable_variants": profitable,
            "total_variants": len(variants)}


def monte_carlo(trades, initial_capital, paths=1000, seed=42, skip_pct=0.0):
    """Reshuffle closed-trade parent nets; rebuild equity from capital.

    Reports median and P5 (worst-case) final equity and P95 max
    drawdown. With skip_pct > 0, a second variant drops each trade with
    that probability (trade-skipping stress). No new trades are invented.
    """
    nets = [t["net"] for t in trades]
    if not nets:
        return {"paths": 0, "median_final": initial_capital,
                "p5_final": initial_capital, "p95_max_dd": 0.0,
                "skip_variant": None}
    rng = random.Random(seed)

    def one_path(skip):
        order = [x for x in nets if skip <= 0 or rng.random() >= skip]
        rng.shuffle(order)
        eq = initial_capital
        peak = eq
        max_dd = 0.0
        for x in order:
            eq += x
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        return eq, max_dd

    finals, dds = [], []
    for _ in range(paths):
        f, d = one_path(0.0)
        finals.append(f)
        dds.append(d)
    finals.sort()
    dds.sort()

    def pct(sorted_vals, p):
        idx = min(len(sorted_vals) - 1, max(0, int(p * len(sorted_vals))))
        return sorted_vals[idx]

    out = {"paths": paths, "seed": seed,
           "median_final": pct(finals, 0.5),
           "p5_final": pct(finals, 0.05),
           "p95_max_dd": pct(dds, 0.95), "skip_variant": None}
    if skip_pct > 0:
        finals2, dds2 = [], []
        for _ in range(paths):
            f, d = one_path(skip_pct)
            finals2.append(f)
            dds2.append(d)
        finals2.sort()
        dds2.sort()
        out["skip_variant"] = {"skip_pct": skip_pct,
                               "median_final": pct(finals2, 0.5),
                               "p5_final": pct(finals2, 0.05),
                               "p95_max_dd": pct(dds2, 0.95)}
    return out


def cost_stress(bars, events, cfg_in):
    """Re-run the same trade list logic at 2x and 3x costs.

    Gap stress is inherent: gap-through-stop fills at the open are
    already in the base run and counted in warnings.gap_fills.
    """
    cfg = validate_config(cfg_in)
    out = []
    import copy
    for mult in (1.0, 2.0, 3.0):
        c = copy.deepcopy(cfg)
        c["commission"]["value"] = float(c["commission"]["value"]) * mult
        c["slippage"] = float(c["slippage"]) * mult
        r = run_backtest(bars, events, c)
        out.append({"cost_multiplier": mult, "net": r["metrics"]["net"],
                    "win_rate": r["metrics"]["win_rate"],
                    "max_dd_usd": r["metrics"]["max_dd_usd"],
                    "trades": r["metrics"]["trades"]})
    return out


# ---------------------------------------------------------------------------
# CSV export + forming-bar helper
# ---------------------------------------------------------------------------

LEDGER_COLUMNS = [
    "parent", "side", "entry_bar", "entry_time", "entry_price",
    "qty_initial", "sl_price", "leg", "leg_fraction", "leg_rr",
    "exit_bar", "exit_time", "exit_price", "leg_qty", "leg_gross",
    "leg_commission", "leg_net", "gap", "reason", "parent_gross",
    "parent_commission", "parent_net", "parent_r", "mfe", "mae",
    "mfe_r", "mae_r", "duration_bars", "final_reason", "force_closed",
    "cumulative_net",
]


def ledger_to_csv(result):
    """One row per leg; parent fields repeated; ALL columns exported."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LEDGER_COLUMNS)
    trades = {t["index"]: t for t in result.get("trades", [])}
    cum = 0.0
    seen_parent = {}
    for leg in result.get("legs", []):
        t = trades.get(leg["parent"], {})
        if leg["parent"] not in seen_parent:
            cum += t.get("net", 0.0)
            seen_parent[leg["parent"]] = cum
        w.writerow([
            leg["parent"], t.get("side", ""), t.get("entry_bar", ""),
            t.get("entry_time", ""), t.get("entry_price", ""),
            t.get("qty_initial", ""), t.get("sl_price", ""),
            leg["leg"], leg.get("fraction", ""), leg.get("rr", ""),
            leg["bar"], leg["time"], leg["price"], leg["qty"],
            round(leg["gross"], 2), round(leg["commission"], 2),
            round(leg["net"], 2), 1 if leg["gap"] else 0, leg["reason"],
            round(t.get("gross", 0.0), 2), round(t.get("commission", 0.0), 2),
            round(t.get("net", 0.0), 2), round(t.get("r", 0.0), 4),
            round(t.get("mfe", 0.0), 2), round(t.get("mae", 0.0), 2),
            round(t.get("mfe_r", 0.0), 4), round(t.get("mae_r", 0.0), 4),
            t.get("duration_bars", ""), t.get("final_reason", ""),
            1 if t.get("force_closed") else 0,
            round(seen_parent[leg["parent"]], 2),
        ])
    return buf.getvalue()


def drop_forming_bar(bars, now_unix, bar_seconds):
    """Drop the forming (incomplete) bar: a bar is forming when its
    close time (open + bar_seconds) is after `now`."""
    out = []
    for b in bars:
        if int(b["time"]) + int(bar_seconds) <= int(now_unix):
            out.append(b)
    return out
