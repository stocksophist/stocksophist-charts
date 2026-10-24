"""Trade Taker regression tests (server-side logic in app/trade_taker.py).

Covers the user's own rules: direction from SL vs entry, market vs
pending order type, the qty floor rule (never above the stated risk),
RR math, and the DEMO backstop (nothing is ever sent without MT5).
Run from the work-tree root: python -m pytest tests/test_trade_taker.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import trade_taker as tt

# XAUUSD fallback facts: tick 0.01, $1 per tick per lot.
TS, TV = 0.01, 1.0


def test_direction():
    assert tt.direction_of(2700, 2696) == "buy"
    assert tt.direction_of(2700, 2704) == "sell"
    assert tt.direction_of(2700, 2700) is None


def test_order_type_market_near_price():
    # BUY entry 5 pts from ask -> within the 10-pt near band -> market.
    assert tt.decide_order_type("buy", 2700.05, 2699.95, 2700.05, 0.01)[0] == "market"
    # SELL entry 5 pts from bid -> market.
    assert tt.decide_order_type("sell", 2699.95, 2699.95, 2700.05, 0.01)[0] == "market"


def test_order_type_pending_standard_rule():
    assert tt.decide_order_type("buy", 2690, 2699.95, 2700.05, 0.01)[0] == "buy_limit"
    assert tt.decide_order_type("buy", 2710, 2699.95, 2700.05, 0.01)[0] == "buy_stop"
    assert tt.decide_order_type("sell", 2710, 2699.95, 2700.05, 0.01)[0] == "sell_limit"
    assert tt.decide_order_type("sell", 2690, 2699.95, 2700.05, 0.01)[0] == "sell_stop"


def test_order_type_respects_stops_level():
    # 15 pts away, but broker stops level is 20 -> still market (a pending
    # trigger that close would be rejected by the broker).
    assert tt.decide_order_type("buy", 2700.15, 2699.95, 2700.05, 0.01,
                                stops_level=20)[0] == "market"
    # Exactly on the 10-pt band -> market (float dust must not flip it).
    assert tt.decide_order_type("buy", 2700.15, 2699.95, 2700.05, 0.01,
                                stops_level=0)[0] == "market"
    # Clearly beyond the band (15 pts) with stops level 0 -> pending.
    assert tt.decide_order_type("buy", 2700.20, 2699.95, 2700.05, 0.01,
                                stops_level=0)[0] == "buy_stop"


def test_order_type_no_quote():
    assert tt.decide_order_type("buy", 2700, None, None, 0.01)[0] is None


def test_qty_floor_user_examples():
    # User's own examples: $4.00 SL distance (2700 -> 2696).
    # Risk $20 -> 0.05 lot (risked $20).
    qty, risked, err = tt.calc_qty(20, 2700, 2696, TS, TV)
    assert (qty, risked, err) == (0.05, 20.0, "")
    # Risk $23 -> 0.05 lot (0.06 would risk $24 > $23, so it floors).
    qty, risked, err = tt.calc_qty(23, 2700, 2696, TS, TV)
    assert (qty, risked, err) == (0.05, 20.0, "")
    assert risked <= 23


def test_qty_never_exceeds_risk():
    for risk in (1, 3, 4, 4.5, 7.99, 8, 11.5, 100, 1000):
        qty, risked, err = tt.calc_qty(risk, 2700, 2696, TS, TV)
        if qty is None:
            assert err == "risk too small for minimum 0.01 lot"
            assert risk < 4  # $4 per 0.01 lot at this SL distance
        else:
            assert risked <= risk
            assert abs(qty * 100 - round(qty * 100)) < 1e-9  # 0.01 steps


def test_qty_too_small_blocked():
    qty, _, err = tt.calc_qty(3, 2700, 2696, TS, TV)
    assert qty is None
    assert "too small" in err


def test_rr_math():
    assert tt.calc_rr(2700, 2696, 2708) == 2.0
    assert tt.calc_rr(2700, 2704, 2692) == 2.0
    assert tt.calc_rr(2700, 2700, 2708) is None


def test_plan_ok_and_tp_side():
    p = tt.plan(2700, 2696, 2708, 20, bid=2699.95, ask=2700.05, point=0.01,
                tick_size=TS, tick_value=TV)
    assert p["ok"] and p["direction"] == "buy" and p["qty"] == 0.05
    assert p["rr"] == 2.0 and p["order_type"] == "market"
    # TP on the wrong side of entry is refused.
    bad = tt.plan(2700, 2696, 2690, 20, bid=2699.95, ask=2700.05, point=0.01,
                  tick_size=TS, tick_value=TV)
    assert not bad["ok"] and "profit side" in bad["message"]


def test_plan_no_quote_still_computes_qty():
    p = tt.plan(2700, 2696, 2708, 20, point=0.01, tick_size=TS, tick_value=TV)
    assert p["ok"] and p["qty"] == 0.05 and p["order_type"] is None


def test_implement_blocked_without_mt5():
    # DEMO backstop: no connection, no MT5 package here -> never sends.
    r = tt.implement_trade(None, {"symbol": "XAUUSD", "entry": 2700,
                                  "sl": 2696, "tp": 2708, "risk": 20})
    assert not r["ok"]
