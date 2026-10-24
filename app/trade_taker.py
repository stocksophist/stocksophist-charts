"""
Trade Taker - risk-sized trade planning from a drawing.

A Trade Taker drawing carries three prices: ENTRY, SL (stop loss) and TP
(target). Everything else is derived, never typed twice:

  * direction: SL below entry = BUY, SL above entry = SELL.
  * order type: entry at/near the live price = MARKET, otherwise a pending
    order (buy_limit / buy_stop / sell_limit / sell_stop, standard rule).
  * quantity: risk$ / (SL distance value for a 0.01 lot), rounded DOWN to
    0.01 steps, NEVER above the stated risk. risk $20 with a $4.00 SL
    distance = 0.05 lot; risk $23 with the same SL = 0.05 lot as well
    (0.06 would risk $24 > $23, so it floors to 0.05 = $20).
  * RR: entry->TP distance divided by entry->SL distance.

Value per distance uses the broker's symbol info when MT5 is live
(tick_size / tick_value); otherwise the XAUUSD fallback (tick_size 0.01,
tick_value 1.00 - i.e. $1 per $1.00 move per 0.01 lot).

Safety rules (non-negotiable):
  * Nothing is ever sent on plot, on drag, or on risk entry. Only an
    explicit Implement sends, and it goes through mt5trade's existing
    market_order / pending_order (same DEMO gate, stops validation,
    margin check and order_check dry-run).
  * The server recomputes quantity from risk on every Implement and
    refuses when even 0.01 lot would exceed the risk ("risk too small
    for minimum 0.01 lot").
  * After Implement, quantity is LOCKED on the drawing object. Later
    SL/TP drags only update the drawing and the RR display - they never
    recompute quantity and never touch the broker order (use the trade
    panel to modify a live order).
"""

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False

import mt5trade

QTY_STEP = 0.01
# "Near the live price" tolerance for the MARKET choice, in MT5 points.
# The broker's stops level is always respected too: a pending trigger
# closer than stops_level would be rejected by the broker, so anything
# inside max(10, stops_level) points is treated as MARKET.
NEAR_POINTS = 10
# Fallback symbol facts (XAUUSD) when MT5 is not connected.
FB_TICK_SIZE = 0.01
FB_TICK_VALUE = 1.0
FB_POINT = 0.01
FB_STOPS_LEVEL = 0
TRADETAKER_MAGIC = 7202  # distinct from panel trades (7201)


def direction_of(entry, sl):
    """'buy' if SL is below entry, 'sell' if above, None if no SL."""
    try:
        entry = float(entry)
        sl = float(sl)
    except (TypeError, ValueError):
        return None
    if sl < entry:
        return "buy"
    if sl > entry:
        return "sell"
    return None


def decide_order_type(direction, entry, bid, ask, point, stops_level=0):
    """Market vs pending from entry vs the live price.

    Returns (order_type, reason). order_type is one of 'market',
    'buy_limit', 'buy_stop', 'sell_limit', 'sell_stop', or None when the
    live price is missing (caller must decide at send time).
    """
    if direction not in ("buy", "sell"):
        return None, "unknown direction"
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return None, "bad entry price"
    ref = None
    if direction == "buy" and ask not in (None, ""):
        ref = float(ask)
    if direction == "sell" and bid not in (None, ""):
        ref = float(bid)
    if ref is None:
        return None, "no live price"
    try:
        point = float(point) or FB_POINT
    except (TypeError, ValueError):
        point = FB_POINT
    near_tol = max(NEAR_POINTS, int(stops_level or 0)) * point
    # epsilon: a 10.00-pt distance must not flip to pending on float dust.
    if abs(entry - ref) <= near_tol + 1e-9:
        return "market", "entry within %d pts of market" % max(
            NEAR_POINTS, int(stops_level or 0))
    if direction == "buy":
        return ("buy_limit" if entry < ref else "buy_stop",
                "entry below market" if entry < ref else "entry above market")
    return ("sell_limit" if entry > ref else "sell_stop",
            "entry above market" if entry > ref else "entry below market")


def risk_for_volume(entry, sl, tick_size, tick_value, volume):
    """Risk in $ if price moves from entry to SL, for a given volume."""
    dist_ticks = abs(float(entry) - float(sl)) / float(tick_size)
    return dist_ticks * float(tick_value) * float(volume)


def calc_qty(risk_usd, entry, sl, tick_size, tick_value):
    """Floor quantity for a risk budget.

    Returns (qty, qty_risk, error). qty is a multiple of 0.01 and its
    risk never exceeds risk_usd. qty is None with an error message when
    even 0.01 lot would exceed the budget.
    """
    try:
        risk_usd = float(risk_usd)
        entry = float(entry)
        sl = float(sl)
        tick_size = float(tick_size)
        tick_value = float(tick_value)
    except (TypeError, ValueError):
        return None, 0.0, "bad numbers"
    if risk_usd <= 0:
        return None, 0.0, "risk must be above 0"
    if entry == sl:
        return None, 0.0, "SL must differ from entry"
    if tick_size <= 0 or tick_value <= 0:
        return None, 0.0, "bad symbol facts"
    risk_at_001 = risk_for_volume(entry, sl, tick_size, tick_value, QTY_STEP)
    if risk_at_001 <= 0:
        return None, 0.0, "bad SL distance"
    # floor: never above the budget (epsilon guards float dust, e.g.
    # 20.0/4.0 landing a hair under 5.0 must still give 5 units, not 4).
    units = int(risk_usd / risk_at_001 + 1e-9)
    if units < 1:
        return None, 0.0, "risk too small for minimum 0.01 lot"
    qty = round(units * QTY_STEP, 2)
    return qty, round(qty * risk_at_001 / QTY_STEP, 2), ""


def calc_rr(entry, sl, tp):
    """Reward:risk ratio, or None when it cannot be computed."""
    try:
        entry = float(entry)
        sl = float(sl)
        tp = float(tp)
    except (TypeError, ValueError):
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return abs(tp - entry) / risk


def plan(entry, sl, tp, risk_usd, bid=None, ask=None, point=None,
         stops_level=0, tick_size=None, tick_value=None):
    """Full preview: direction, order type, qty, RR. Pure - no MT5 use.

    tick_size/tick_value fall back to the XAUUSD rule when omitted.
    """
    direction = direction_of(entry, sl)
    if direction is None:
        return {"ok": False, "message": "SL must differ from entry"}
    try:
        risk_usd = float(risk_usd)
    except (TypeError, ValueError):
        return {"ok": False, "message": "risk must be a number"}
    if risk_usd <= 0:
        return {"ok": False, "message": "risk must be above 0"}
    ts = float(tick_size) if tick_size else FB_TICK_SIZE
    tv = float(tick_value) if tick_value else FB_TICK_VALUE
    qty, qty_risk, err = calc_qty(risk_usd, entry, sl, ts, tv)
    if qty is None:
        return {"ok": False, "message": err}
    otype, reason = decide_order_type(direction, entry, bid, ask,
                                      point or FB_POINT, stops_level)
    rr = calc_rr(entry, sl, tp)
    if tp in (None, ""):
        return {"ok": False, "message": "TP is required"}
    try:
        tp = float(tp)
    except (TypeError, ValueError):
        return {"ok": False, "message": "TP must be a number"}
    # TP must sit on the profit side of entry, else RR is meaningless.
    if (direction == "buy" and tp <= float(entry)) or \
            (direction == "sell" and tp >= float(entry)):
        return {"ok": False,
                "message": "TP must be on the profit side of entry"}
    return {"ok": True, "direction": direction, "order_type": otype,
            "order_type_reason": reason, "qty": qty, "qty_risk": qty_risk,
            "risk": risk_usd, "rr": rr,
            "value_source": "broker" if tick_size else "fallback"}


def implement_trade(conn, payload):
    """Send a Trade Taker order through mt5trade. Only called on Implement.

    payload: symbol, entry, sl, tp, risk, bid, ask (client live quote).
    Returns the mt5trade result dict, extended with the planned values.
    """
    ok, err = mt5trade._need(conn)
    if not ok:
        return {"ok": False, "message": err}
    symbol = (payload.get("symbol") or "").strip() or "XAUUSD"
    try:
        entry = float(payload.get("entry"))
        sl = float(payload.get("sl"))
        tp = float(payload.get("tp"))
        risk = float(payload.get("risk"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "entry, SL, TP and risk are required"}
    info, err = mt5trade._sym(symbol)
    if info is None:
        return {"ok": False, "message": err}
    tick = mt5trade._tick(symbol)
    bid = float(tick.bid) if tick else payload.get("bid")
    ask = float(tick.ask) if tick else payload.get("ask")
    prev = plan(entry, sl, tp, risk, bid=bid, ask=ask, point=info.point,
                stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
                tick_size=info.tick_size, tick_value=info.tick_value)
    if not prev["ok"]:
        return {"ok": False, "message": prev["message"]}
    if prev["order_type"] is None:
        return {"ok": False, "message": "no live price - cannot decide order type"}
    point = float(info.point) or FB_POINT
    sl_pts = abs(entry - sl) / point
    tp_pts = abs(tp - entry) / point
    if prev["order_type"] == "market":
        res = mt5trade.market_order(symbol, prev["direction"], prev["qty"],
                                    sl_pts=sl_pts, tp_pts=tp_pts,
                                    magic=TRADETAKER_MAGIC,
                                    comment="tradetaker")
    else:
        res = mt5trade.pending_order(symbol, prev["order_type"], prev["qty"],
                                     entry, sl_pts=sl_pts, tp_pts=tp_pts,
                                     magic=TRADETAKER_MAGIC,
                                     comment="tradetaker")
    res.update({"direction": prev["direction"],
                "order_type": prev["order_type"], "qty": prev["qty"],
                "qty_risk": prev["qty_risk"], "rr": prev["rr"],
                "entry": entry, "sl": sl, "tp": tp})
    return res


def register_tradetaker_routes(app, conn):
    """Mount /api/trade_taker/* on the chart Flask app."""
    from flask import jsonify, request

    @app.route("/api/trade_taker/implement", methods=["POST"])
    def tradetaker_implement():
        b = request.get_json(force=True, silent=True) or {}
        # Dry run: pure math preview, no MT5 needed, nothing is sent.
        if b.get("dry_run"):
            try:
                entry = float(b.get("entry"))
                sl = float(b.get("sl"))
                tp = float(b.get("tp"))
                risk = float(b.get("risk"))
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "message": "entry, SL, TP and risk are required"}), 400
            prev = plan(entry, sl, tp, risk, bid=b.get("bid"),
                        ask=b.get("ask"), point=b.get("point"),
                        stops_level=int(b.get("stops_level") or 0),
                        tick_size=b.get("tick_size"),
                        tick_value=b.get("tick_value"))
            prev["dry_run"] = True
            return jsonify(prev), 200 if prev["ok"] else 400
        try:
            return jsonify(implement_trade(conn, b))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500
