"""
Direct chart trading on MT5 - market/pending orders, SL/TP modify, close.

Safety rules (non-negotiable):
  * Every route refuses when the MT5 connection is not live (DEMO mode can
    never send an order). The frontend also blocks, this is the backstop.
  * All order_send calls hold TRADE_LOCK (single pipe to the terminal).
  * Volumes are snapped to the broker's volume_step and clamped to
    [volume_min, volume_max] before sending.
  * SL/TP and pending trigger prices are pre-validated against the broker's
    trade_stops_level (points) so 10016 becomes a friendly message, not a
    silent reject.
  * Every response carries {ok, retcode, message} - the UI shows the exact
    broker retcode/comment on failure, never a generic "failed".
  * Panel trades use magic 7201 so they never collide with other automated trades.

Implements MetaQuotes-documented pending/modify/remove actions.
"""

import threading
import time

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False


TRADE_LOCK = threading.Lock()
PANEL_MAGIC = 7201          # chart-panel trades
_DEVIATION = 20              # points, market orders
_CLOSE_DEVIATION = 30       # points, closes


# --------------------------------------------------------------- retcodes
# Verified against mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes
_RETCODE_TEXT = {
    10009: "Done",
    10008: "Order placed",
    10010: "Partially filled",
    10004: "Requote - price moved",
    10020: "Prices changed",
    10021: "No quotes from broker",
    10006: "Rejected by server",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stops (SL/TP too close)",
    10017: "Trading disabled",
    10018: "Market closed",
    10019: "Not enough money",
    10022: "Invalid expiration",
    10024: "Too many requests - slow down",
    10025: "No changes",
    10026: "Auto-trading disabled by server",
    10027: "Algo Trading disabled in MT5 terminal (Ctrl+E)",
    10029: "Order/position frozen (too close to market)",
    10030: "Filling mode rejected by server",
    10031: "No connection to trade server",
    10033: "Pending-order limit reached",
    10035: "Order type not allowed on this symbol",
    10036: "Position already closed",
    10039: "Close-by opposite position exists",
    10045: "FIFO account rule violation",
    10046: "Hedging prohibited on this account",
    10038: "Invalid close volume",
}


def retcode_message(res):
    """Human-readable broker outcome. res may be None."""
    if res is None:
        return "no response from MT5"
    base = _RETCODE_TEXT.get(res.retcode, "retcode %d" % res.retcode)
    comment = (getattr(res, "comment", "") or "").strip()
    if comment and comment.lower() not in base.lower():
        return "%s (%s)" % (base, comment)
    return base


# --------------------------------------------------------------- guards
def _need(conn):
    """(ok, error). DEMO / disconnected can never trade - backstop."""
    if not HAS_MT5:
        return False, ("MetaTrader5 package not installed on this machine. "
                       "Trading works only on Windows.")
    if conn is None or not conn.is_live_fresh():
        return False, "MT5 not connected - trading disabled (DEMO mode)."
    return True, ""


def _sym(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return None, "unknown symbol %s" % symbol
    if not info.visible:
        try:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        except Exception:
            pass
    return info, ""


def snap_volume(info, volume):
    """Snap to broker step, clamp to [min, max]. Returns (vol, note)."""
    try:
        vol = float(volume)
    except (TypeError, ValueError):
        return None, "bad volume"
    step = info.volume_step or 0.01
    vmin = info.volume_min or 0.01
    vmax = info.volume_max or 100.0
    vol = round(round(vol / step) * step, 8)
    if vol < vmin:
        return vmin, "raised to broker minimum %.2f" % vmin
    if vol > vmax:
        return vmax, "capped to broker maximum %.2f" % vmax
    return vol, ""


def _filling_for_market(info):
    """Market-order filling mode from the broker's bitmask (FOK -> IOC -> RETURN)."""
    fm = int(getattr(info, "filling_mode", 2) or 2)
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def stops_ok(info, side, ref, sl=0.0, tp=0.0):
    """Pre-validate SL/TP distance vs broker stops level (points -> price)."""
    level = int(getattr(info, "trade_stops_level", 0) or 0)
    if level <= 0:
        return True, ""
    min_dist = level * info.point
    if sl:
        bad = (ref - sl) < min_dist if side == "buy" else (sl - ref) < min_dist
        if bad:
            return False, "SL too close to price: broker needs %d pts" % level
    if tp:
        bad = (tp - ref) < min_dist if side == "buy" else (ref - tp) < min_dist
        if bad:
            return False, "TP too close to price: broker needs %d pts" % level
    return True, ""


def check_margin(symbol, side, volume, price):
    action = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    try:
        margin = mt5.order_calc_margin(action, symbol, float(volume), float(price))
    except Exception:
        margin = None
    if margin is None:
        return False, "broker margin check failed"
    try:
        acc = mt5.account_info()
        free = float(acc.margin_free) if acc else 0.0
    except Exception:
        free = 0.0
    if margin > free:
        return False, "need $%.2f margin, free $%.2f - reduce size" % (margin, free)
    return True, ""


def _validate(req):
    """Dry-run the exact request dict via order_check."""
    try:
        chk = mt5.order_check(req)
    except Exception as e:
        return False, "order_check error: %s" % e
    if chk is None:
        return False, "order_check returned nothing"
    if int(getattr(chk, "retcode", -1)) != 0:
        return False, "broker check: %s (code %s)" % (
            getattr(chk, "comment", "?"), getattr(chk, "retcode", "?"))
    return True, ""


def _tick(symbol):
    t = mt5.symbol_info_tick(symbol)
    return t


# --------------------------------------------------------------- orders
def market_order(symbol, side, volume, sl_pts=0.0, tp_pts=0.0,
                 magic=PANEL_MAGIC, comment="chart"):
    """Market BUY/SELL with optional SL/TP in points from fill price."""
    info, err = _sym(symbol)
    if info is None:
        return {"ok": False, "message": err}
    volume, note = snap_volume(info, volume)
    if volume is None:
        return {"ok": False, "message": note}
    tick = _tick(symbol)
    if tick is None:
        return {"ok": False, "message": "no price feed for %s" % symbol}
    price = float(tick.ask if side == "buy" else tick.bid)
    digits = info.digits
    sl = round(price - float(sl_pts) * info.point, digits) if sl_pts else 0.0
    tp = round(price + float(tp_pts) * info.point, digits) if tp_pts else 0.0
    if side == "sell":
        sl = round(price + float(sl_pts) * info.point, digits) if sl_pts else 0.0
        tp = round(price - float(tp_pts) * info.point, digits) if tp_pts else 0.0
    ok, msg = stops_ok(info, side, price, sl, tp)
    if not ok:
        return {"ok": False, "message": msg}
    ok, msg = check_margin(symbol, side, volume, price)
    if not ok:
        return {"ok": False, "message": msg}
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
        "price": float(price),
        "sl": float(sl), "tp": float(tp),
        "deviation": _DEVIATION,
        "magic": int(magic),
        "comment": str(comment)[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_for_market(info),
    }
    ok, msg = _validate(req)
    if not ok:
        return {"ok": False, "message": msg}
    with TRADE_LOCK:
        res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "ticket": int(res.order), "deal": int(res.deal),
                "retcode": int(res.retcode), "price": float(res.price),
                "message": "Done - position #%d%s" % (
                    int(res.order), (" (%s)" % note) if note else "")}
    # one retry on requote/price-changed with a fresh tick
    if res is not None and res.retcode in (10004, 10020):
        tick = _tick(symbol)
        if tick is not None:
            req["price"] = float(tick.ask if side == "buy" else tick.bid)
            with TRADE_LOCK:
                res = mt5.order_send(req)
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                return {"ok": True, "ticket": int(res.order),
                        "deal": int(res.deal),
                        "retcode": int(res.retcode), "price": float(res.price),
                        "message": "Done after requote - position #%d" % int(res.order)}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


_PENDING_TYPES = {
    "buy_limit": "ORDER_TYPE_BUY_LIMIT",
    "sell_limit": "ORDER_TYPE_SELL_LIMIT",
    "buy_stop": "ORDER_TYPE_BUY_STOP",
    "sell_stop": "ORDER_TYPE_SELL_STOP",
    "buy_stop_limit": "ORDER_TYPE_BUY_STOP_LIMIT",
    "sell_stop_limit": "ORDER_TYPE_SELL_STOP_LIMIT",
}


def pending_order(symbol, otype, volume, price, sl_pts=0.0, tp_pts=0.0,
                  stoplimit=0.0, magic=PANEL_MAGIC, comment="chart"):
    """Pending order. otype: buy_limit|sell_limit|buy_stop|sell_stop|
    buy_stop_limit|sell_stop_limit. SL/TP in points from the trigger price."""
    key = (otype or "").lower()
    if key not in _PENDING_TYPES:
        return {"ok": False, "message": "unknown pending type %r" % otype}
    info, err = _sym(symbol)
    if info is None:
        return {"ok": False, "message": err}
    volume, note = snap_volume(info, volume)
    if volume is None:
        return {"ok": False, "message": note}
    tick = _tick(symbol)
    if tick is None:
        return {"ok": False, "message": "no price feed for %s" % symbol}
    digits = info.digits
    price = round(float(price), digits)
    # trigger must respect stops-level distance from current price
    side = "buy" if key.startswith("buy") else "sell"
    ref = float(tick.ask if side == "buy" else tick.bid)
    min_dist = int(getattr(info, "trade_stops_level", 0) or 0) * info.point
    if min_dist and abs(price - ref) < min_dist:
        return {"ok": False,
                "message": "trigger too close to market: broker needs %d pts" %
                int(getattr(info, "trade_stops_level", 0))}
    sl = round(price - float(sl_pts) * info.point, digits) if sl_pts else 0.0
    tp = round(price + float(tp_pts) * info.point, digits) if tp_pts else 0.0
    if side == "sell":
        sl = round(price + float(sl_pts) * info.point, digits) if sl_pts else 0.0
        tp = round(price - float(tp_pts) * info.point, digits) if tp_pts else 0.0
    ok, msg = stops_ok(info, side, price, sl, tp)
    if not ok:
        return {"ok": False, "message": msg}
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": float(volume),
        "type": getattr(mt5, _PENDING_TYPES[key]),
        "price": float(price),
        "sl": float(sl), "tp": float(tp),
        "magic": int(magic),
        "comment": str(comment)[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        # pendings MUST use RETURN filling (docs) - never IOC/FOK
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    if key in ("buy_stop_limit", "sell_stop_limit"):
        if not stoplimit:
            return {"ok": False,
                    "message": "stop-limit needs a limit price (stoplimit)"}
        req["stoplimit"] = round(float(stoplimit), digits)
    with TRADE_LOCK:
        res = mt5.order_send(req)
    if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE,
                                           mt5.TRADE_RETCODE_PLACED):
        return {"ok": True, "order": int(res.order),
                "retcode": int(res.retcode),
                "message": "Placed - order #%d%s" % (
                    int(res.order), (" (%s)" % note) if note else "")}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


def modify_position(ticket, sl=None, tp=None):
    """Change SL/TP on an open position (drag-SL/TP primitive)."""
    poss = mt5.positions_get(ticket=int(ticket))
    if not poss:
        return {"ok": False, "message": "position #%s not found" % ticket}
    pos = poss[0]
    new_sl = float(sl) if sl not in (None, "") else float(pos.sl)
    new_tp = float(tp) if tp not in (None, "") else float(pos.tp)
    if abs(new_sl - float(pos.sl)) < 1e-9 and abs(new_tp - float(pos.tp)) < 1e-9:
        return {"ok": True, "message": "no changes", "unchanged": True}
    info, err = _sym(pos.symbol)
    if info is None:
        return {"ok": False, "message": err}
    side = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
    tick = _tick(pos.symbol)
    ref = float(tick.ask if side == "buy" else tick.bid) if tick else None
    if ref:
        ok, msg = stops_ok(info, side, ref, new_sl, new_tp)
        if not ok:
            return {"ok": False, "message": msg}
    req = {"action": mt5.TRADE_ACTION_SLTP,
           "position": int(ticket), "symbol": pos.symbol,
           "sl": new_sl, "tp": new_tp}
    with TRADE_LOCK:
        res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "retcode": int(res.retcode),
                "message": "Position #%d updated" % int(ticket)}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


def modify_pending(order, price=None, sl=None, tp=None):
    """Change trigger/SL/TP on a pending order (TRADE_ACTION_MODIFY)."""
    cur = mt5.orders_get(ticket=int(order))
    if not cur:
        return {"ok": False, "message": "pending order #%s not found" % order}
    o = cur[0]
    new_price = round(float(price), o.digits if hasattr(o, "digits") else 2) \
        if price not in (None, "") else float(o.price_open)
    new_sl = float(sl) if sl not in (None, "") else float(o.sl)
    new_tp = float(tp) if tp not in (None, "") else float(o.tp)
    req = {"action": mt5.TRADE_ACTION_MODIFY,
           "order": int(order), "symbol": o.symbol,
           "price": new_price, "sl": new_sl, "tp": new_tp,
           "deviation": _DEVIATION}
    with TRADE_LOCK:
        res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "retcode": int(res.retcode),
                "message": "Order #%d updated" % int(order)}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


def delete_pending(order):
    with TRADE_LOCK:
        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE,
                              "order": int(order)})
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "message": "Order #%d deleted" % int(order)}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


def close_position(ticket, volume=None, comment="chart_close"):
    """Close a position; pass a smaller volume for a partial close."""
    poss = mt5.positions_get(ticket=int(ticket))
    if not poss:
        return {"ok": False, "message": "position #%s not found" % ticket}
    pos = poss[0]
    tick = _tick(pos.symbol)
    if tick is None:
        return {"ok": False, "message": "no price feed for %s" % pos.symbol}
    is_buy = (pos.type == mt5.ORDER_TYPE_BUY)
    opposite = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    price = float(tick.bid if is_buy else tick.ask)
    info, _ = _sym(pos.symbol)
    vol = float(pos.volume)
    if volume not in (None, "", 0):
        v, _ = snap_volume(info, min(float(volume), vol)) if info else (float(volume), "")
        vol = min(v, vol)
    req = {"action": mt5.TRADE_ACTION_DEAL,
           "position": int(pos.ticket), "symbol": pos.symbol,
           "volume": float(vol), "type": opposite, "price": price,
           "deviation": _CLOSE_DEVIATION, "magic": int(pos.magic),
           "comment": str(comment)[:31], "type_time": mt5.ORDER_TIME_GTC,
           "type_filling": _filling_for_market(info) if info else mt5.ORDER_FILLING_IOC}
    with TRADE_LOCK:
        res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        return {"ok": True, "retcode": int(res.retcode),
                "message": "Closed %s of #%d" % (
                    ("%.2f" % vol) if vol < float(pos.volume) else "position",
                    int(pos.ticket))}
    return {"ok": False, "retcode": int(res.retcode) if res else None,
            "message": retcode_message(res)}


# --------------------------------------------------------------- reads
def _pos_dict(pos):
    live = float(pos.profit) + float(pos.swap)
    return {"ticket": int(pos.ticket), "symbol": pos.symbol,
            "type": "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
            "volume": float(pos.volume), "price_open": float(pos.price_open),
            "sl": float(pos.sl), "tp": float(pos.tp),
            "profit": float(pos.profit), "swap": float(pos.swap),
            "commission": float(getattr(pos, "commission", 0.0) or 0.0),
            "pnl": live, "magic": int(pos.magic),
            "time": int(getattr(pos, "time", 0) or 0)}


def list_positions(symbol=None):
    poss = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    return [_pos_dict(p) for p in (poss or [])]


_OTYPE_NAMES = {0: "buy", 1: "sell", 2: "buy_limit", 3: "sell_limit",
                4: "buy_stop", 5: "sell_stop", 6: "buy_stop_limit",
                7: "sell_stop_limit"}


def list_orders(symbol=None):
    oo = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    out = []
    for o in (oo or []):
        out.append({"ticket": int(o.ticket), "symbol": o.symbol,
                    "type": _OTYPE_NAMES.get(int(o.type), str(o.type)),
                    "volume": float(o.volume_initial),
                    "price": float(o.price_open), "sl": float(o.sl),
                    "tp": float(o.tp), "magic": int(o.magic),
                    "time": int(getattr(o, "time_setup", 0) or 0)})
    return out


def account_brief():
    acc = mt5.account_info()
    if acc is None:
        return None
    return {"login": acc.login, "server": acc.server, "name": acc.name,
            "balance": float(acc.balance), "equity": float(acc.equity),
            "margin_free": float(acc.margin_free),
            "currency": acc.currency,
            "trade_allowed": bool(getattr(acc, "trade_allowed", True))}


# --------------------------------------------------------------- routes
def register_trade_routes(app, conn):
    """Mount /api/trade/* on the chart Flask app."""
    from flask import jsonify, request

    def gate():
        ok, err = _need(conn)
        if not ok:
            return jsonify({"ok": False, "demo": True, "message": err}), 503
        return None

    @app.route("/api/trade/market", methods=["POST"])
    def trade_market():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        side = (b.get("side") or "").lower()
        if side not in ("buy", "sell"):
            return jsonify({"ok": False, "message": "side must be buy|sell"}), 400
        symbol = (b.get("symbol") or "").strip() or "XAUUSD"
        try:
            return jsonify(market_order(
                symbol, side, b.get("volume", 0.01),
                sl_pts=float(b.get("sl_pts") or 0),
                tp_pts=float(b.get("tp_pts") or 0),
                magic=int(b.get("magic") or PANEL_MAGIC),
                comment=b.get("comment") or "chart"))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/pending", methods=["POST"])
    def trade_pending():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        symbol = (b.get("symbol") or "").strip() or "XAUUSD"
        try:
            return jsonify(pending_order(
                symbol, b.get("otype"), b.get("volume", 0.01),
                b.get("price", 0),
                sl_pts=float(b.get("sl_pts") or 0),
                tp_pts=float(b.get("tp_pts") or 0),
                stoplimit=float(b.get("stoplimit") or 0),
                magic=int(b.get("magic") or PANEL_MAGIC),
                comment=b.get("comment") or "chart"))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/modify_position", methods=["POST"])
    def trade_modify_position():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(modify_position(b.get("ticket"),
                                           b.get("sl"), b.get("tp")))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/modify_pending", methods=["POST"])
    def trade_modify_pending():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(modify_pending(b.get("order"), b.get("price"),
                                          b.get("sl"), b.get("tp")))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/delete_pending", methods=["POST"])
    def trade_delete_pending():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(delete_pending(b.get("order")))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/close", methods=["POST"])
    def trade_close():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(close_position(b.get("ticket"), b.get("volume"),
                                          b.get("comment") or "chart_close"))
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/close_all", methods=["POST"])
    def trade_close_all():
        g = gate()
        if g:
            return g
        b = request.get_json(force=True, silent=True) or {}
        symbol = (b.get("symbol") or "").strip() or None
        closed, failed = [], []
        try:
            for p in list_positions(symbol):
                r = close_position(p["ticket"], comment="chart_flat")
                (closed if r["ok"] else failed).append(p["ticket"])
            # also cancel pending orders on the symbol
            for o in list_orders(symbol):
                r = delete_pending(o["ticket"])
                (closed if r["ok"] else failed).append(o["ticket"])
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500
        return jsonify({"ok": not failed, "closed": closed, "failed": failed,
                        "message": "Closed/cancelled %d%s" % (
                            len(closed), ("; failed: %s" % failed) if failed else "")})

    @app.route("/api/trade/positions")
    def trade_positions():
        g = gate()
        if g:
            return g
        symbol = (request.args.get("symbol") or "").strip() or None
        try:
            return jsonify({"ok": True, "positions": list_positions(symbol)})
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/orders")
    def trade_orders():
        g = gate()
        if g:
            return g
        symbol = (request.args.get("symbol") or "").strip() or None
        try:
            return jsonify({"ok": True, "orders": list_orders(symbol)})
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/account")
    def trade_account():
        g = gate()
        if g:
            return g
        try:
            a = account_brief()
            return jsonify({"ok": True, "account": a})
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500

    @app.route("/api/trade/symbol_info")
    def trade_symbol_info():
        """Broker constraints for the panel: digits, point, stops level,
        volume min/max/step, filling mode. Pre-validates client-side."""
        g = gate()
        if g:
            return g
        symbol = (request.args.get("symbol") or "").strip() or "XAUUSD"
        try:
            info, err = _sym(symbol)
            if info is None:
                return jsonify({"ok": False, "message": err}), 400
            return jsonify({"ok": True, "symbol": symbol,
                            "digits": info.digits, "point": info.point,
                            "stops_level": int(info.trade_stops_level or 0),
                            "freeze_level": int(info.trade_freeze_level or 0),
                            "volume_min": info.volume_min,
                            "volume_max": info.volume_max,
                            "volume_step": info.volume_step,
                            "tick_value": info.tick_value,
                            "tick_size": info.tick_size})
        except Exception as e:
            return jsonify({"ok": False, "message": "error: %s" % e}), 500
