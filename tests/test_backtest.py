"""Backtest engine tests (GitHub build, simulation only).

Run from the work-tree root:
    python -m pytest tests/test_backtest.py -q
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import backtest as bt


def bar(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def flat(prices, spread=0.5, t0=0, step=60):
    """Bars from a close series: open = close, high/low = close +/- spread."""
    return [bar(t0 + i * step, p, p + spread, p - spread, p)
            for i, p in enumerate(prices)]


def cfg(**over):
    base = {
        "symbol": "XAUUSD", "tf": "1m", "bar_seconds": 60,
        "initial_capital": 10000.0, "direction": "both",
        "sizing": {"mode": "fixed", "fixed_qty": 0.10},
        "sl": {"mode": "price", "value": 5.0},
        "tp_legs": [], "playbook": False,
        "trailing": {"type": "none"},
        "breakeven_after_tp1": False, "c2c_timing": "next_open",
        "opposite_exit": "close", "max_bars": None,
        "close_exit_bars": None,
        "commission": {"type": "per_order", "value": 0.50},
        "slippage": 0.10, "point": 0.01, "tick_value": 1.0,
        "warmup_bars": 0,
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = dict(base[k], **v)
        else:
            base[k] = v
    return base


def ev(bi, side):
    return {"bar_index": bi, "time": bi * 60, "side": side}


def test_sl_first_same_bar():
    # Signal bar 0, fill bar 1 at open 100 (+0.1 slip) = 100.1.
    # SL 95.1, TP (rr 1) 105.1. Bar 2 touches both -> SL wins.
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),
            bar(120, 100.5, 106, 94, 100)]
    res = bt.run_backtest(bars, [ev(0, "BUY")],
                          cfg(tp_legs=[{"rr": 1.0, "fraction": 1.0}]))
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["final_reason"] == "StopLoss"
    assert abs(t["legs"][0]["price"] - 95.0) < 1e-9


def test_gap_through_stop_at_open():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),
            bar(120, 90, 91, 89, 90.5)]  # gaps below SL 95.1
    res = bt.run_backtest(bars, [ev(0, "BUY")], cfg())
    t = res["trades"][0]
    assert t["final_reason"] == "GapExit"
    assert abs(t["legs"][0]["price"] - 90.0) < 1e-9  # at open, no slippage
    assert res["warnings"]["gap_fills"] == 1


def test_tp1_partial_then_runner_continues():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),
            bar(120, 101, 116, 100, 115),   # TP1 115.1 fills
            bar(180, 115, 115, 96, 97),     # runner survives (low 96 > 95.1)
            bar(240, 97, 98, 94, 95)]       # runner stops out
    res = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(tp_legs=[{"rr": 3.0, "fraction": 0.75}]))
    t = res["trades"][0]
    assert len(t["legs"]) == 2
    assert t["legs"][0]["reason"] == "TakeProfit1"
    assert abs(t["legs"][0]["qty"] - 0.07) < 1e-9
    assert t["legs"][1]["reason"] == "StopLoss"
    assert abs(t["legs"][1]["qty"] - 0.03) < 1e-9
    assert t["final_reason"] == "StopLoss"


def test_trailing_ratchet_never_moves_back():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),     # fill 100.1
            bar(120, 100.5, 110, 104, 109),   # peak 110 -> trail 105
            bar(180, 109, 107, 106, 106.5),   # dip, survives, trail stays 105
            bar(240, 106.5, 112, 111, 111.5),  # peak 112 -> trail 107
            bar(300, 111.5, 108, 106.5, 107)]  # low 106.5 <= 107 -> trail exit
    res = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(trailing={"type": "fixed", "distance": 5.0}))
    t = res["trades"][0]
    assert t["final_reason"] == "TrailingStop"
    assert t["exit_bar"] == 5
    assert abs(t["legs"][0]["price"] - 106.9) < 1e-9


def test_breakeven_after_tp1():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),   # fill 100.1, TP1 105.1
            bar(120, 101, 106, 100, 105),   # TP1 fills half
            bar(180, 105, 105, 99, 100)]    # back to entry -> breakeven
    res = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(tp_legs=[{"rr": 1.0, "fraction": 0.5}],
            breakeven_after_tp1=True))
    t = res["trades"][0]
    assert len(t["legs"]) == 2
    assert t["legs"][1]["reason"] == "BreakevenStop"
    assert abs(t["legs"][1]["price"] - 100.0) < 1e-9  # entry - slippage


def test_c2c_next_open_vs_same_close():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 101),
            bar(120, 101, 102, 100, 102),
            bar(180, 102, 103, 101, 103),
            bar(240, 103, 104, 102, 104),
            bar(300, 104, 105, 103, 105)]
    events = [ev(0, "BUY"), ev(3, "SELL")]
    r_next = bt.run_backtest(bars, events, cfg(sl={"value": 50.0}))
    t = r_next["trades"][0]
    assert t["final_reason"] == "OppositeSignal"
    assert t["exit_bar"] == 4
    assert abs(t["legs"][0]["price"] - 102.9) < 1e-9  # bar 4 open - slip
    r_same = bt.run_backtest(bars, events,
                             cfg(sl={"value": 50.0},
                                 c2c_timing="same_close"))
    t2 = r_same["trades"][0]
    assert t2["final_reason"] == "OppositeSignal"
    assert t2["exit_bar"] == 3
    assert abs(t2["legs"][0]["price"] - 103.0) < 1e-9  # bar 3 close, no slip


def test_opposite_reverse_opens_opposite():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 101),
            bar(120, 101, 102, 100, 102),
            bar(180, 102, 103, 101, 103),
            bar(240, 103, 104, 102, 104),
            bar(300, 104, 105, 103, 105)]
    res = bt.run_backtest(bars, [ev(0, "BUY"), ev(3, "SELL")],
                          cfg(sl={"value": 50.0}, opposite_exit="reverse"))
    assert len(res["trades"]) == 2
    assert res["trades"][0]["side"] == "long"
    assert res["trades"][1]["side"] == "short"
    assert abs(res["trades"][1]["entry_price"] - 102.9) < 1e-9
    assert res["trades"][1]["final_reason"] == "EndOfRange"


def test_force_close_flag():
    bars = flat([100, 101, 102, 103, 104, 105])
    res = bt.run_backtest(bars, [ev(0, "BUY")], cfg(sl={"value": 50.0}))
    t = res["trades"][0]
    assert t["final_reason"] == "EndOfRange"
    assert t["force_closed"] is True
    assert res["warnings"]["force_closes"] == 1
    assert abs(t["legs"][0]["price"] - 105.0) < 1e-9


def test_sizing_floor_never_above_risk():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 101),
            bar(120, 101, 102, 100, 102)]
    # $23 risk, $4 SL distance -> $400 risk per lot -> 0.0575 -> floor 0.05
    res = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(sizing={"mode": "risk", "risk_usd": 23.0},
            sl={"value": 4.0}, slippage=0.0))
    t = res["trades"][0]
    assert abs(t["qty_initial"] - 0.05) < 1e-9
    assert t["planned_risk"] <= 23.0 + 1e-9
    # $3 risk is too small for 0.01 lot -> refused, never above risk
    res2 = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(sizing={"mode": "risk", "risk_usd": 3.0},
            sl={"value": 4.0}, slippage=0.0))
    assert res2["trades"] == []
    assert len(res2["warnings"]["skips"]) == 1


def test_commission_and_slippage_on_every_fill():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),   # fill 100.1
            bar(120, 100.5, 101, 94, 95)]   # SL exit at 95.0
    res = bt.run_backtest(bars, [ev(0, "BUY")], cfg())
    t = res["trades"][0]
    # entry 0.50 + exit 0.50 = 1.00; pnl (95.0-100.1)*0.10*100 = -51.0
    assert abs(t["commission"] - 1.0) < 1e-9
    assert abs(t["net"] - (-52.0)) < 1e-9
    assert abs(sum(x["commission"] for x in t["legs"]) - 1.0) < 1e-9
    # TP partial + runner: 3 fills -> 1.50 commission
    bars2 = [bar(0, 100, 100.5, 99.5, 100),
             bar(60, 100, 101, 99, 100.5),
             bar(120, 101, 106, 100, 105),
             bar(180, 105, 105, 94, 95)]
    res2 = bt.run_backtest(
        bars2, [ev(0, "BUY")],
        cfg(tp_legs=[{"rr": 1.0, "fraction": 0.5}]))
    t2 = res2["trades"][0]
    assert abs(t2["commission"] - 1.5) < 1e-9


def test_forming_bar_exclusion():
    bars = [bar(0, 100, 101, 99, 100), bar(60, 100, 101, 99, 100),
            bar(120, 100, 101, 99, 100), bar(180, 100, 101, 99, 100)]
    kept = bt.drop_forming_bar(bars, now_unix=200, bar_seconds=60)
    assert [b["time"] for b in kept] == [0, 60, 120]  # 180+60 > 200: forming


def test_repaint_gate_pass_and_fail():
    bars = flat(list(range(100, 85, -1)) + list(range(85, 111)))
    events, warm = bt.supertrend_flip_events(bars, {"length": 10, "mult": 3.0})
    assert warm == 10
    assert events, "expected at least one flip in a fall-then-rise series"
    for a, b in zip(events, events[1:]):
        assert a["side"] != b["side"]  # flips alternate
    # First real state is at index 10; a flip needs a prior real state.
    assert all(e["bar_index"] > 10 for e in events)
    gate = bt.repaint_gate(
        lambda bs: bt.supertrend_flip_events(bs, {"length": 10, "mult": 3.0}),
        bars)
    assert gate["passed"] is True

    def lookahead_provider(bs):
        # Uses bar 30 (future) to decide an event at bar 5: repainting.
        # Full history: bar 30 close 100 > bar 5 close 95 -> event.
        # Any truncation before bar 30: no event -> prefix mismatch.
        if len(bs) > 30 and bs[30]["close"] > bs[5]["close"]:
            return [{"bar_index": 5, "time": bs[5]["time"], "side": "BUY"}], 0
        return [], 0
    bad = bt.repaint_gate(lookahead_provider, bars)
    assert bad["passed"] is False
    assert bad["first_failing_bar"] == 5


def test_walk_forward_structure():
    bars = flat([100 + i * 0.5 for i in range(40)])
    events = [ev(2, "BUY"), ev(10, "SELL"), ev(25, "BUY")]
    wf = bt.walk_forward(bars, events, cfg(sl={"value": 50.0}),
                         split_ratio=0.5)
    assert wf["split_bar"] == 20
    assert "metrics" in wf["in_sample"] and "metrics" in wf["out_of_sample"]
    # No trade spans slices: every oos trade entry bar is slice-local.
    for t in wf["out_of_sample"]["trades"]:
        assert 0 <= t["entry_bar"] < 20


def test_parameter_sweep_and_stability_share():
    bars = flat([100, 101, 102, 103, 104, 105, 104, 103, 102, 101,
                 100, 101, 102, 103, 104, 105, 106, 107])
    events = [ev(0, "BUY")]
    sw = bt.parameter_sweep(
        bars, events,
        cfg(tp_legs=[{"rr": 2.0, "fraction": 0.5}],
            trailing={"type": "fixed", "distance": 5.0}))
    assert sw["total_variants"] > 0
    assert 0.0 <= sw["stability_share"] <= 1.0
    assert all("net" in v for v in sw["variants"])


def test_monte_carlo_bounds():
    bars = flat([100, 102, 101, 103, 102, 104, 103, 105, 104, 106,
                 105, 107, 106, 108, 107, 109])
    events = [ev(0, "BUY"), ev(4, "SELL"), ev(8, "BUY"), ev(12, "SELL")]
    res = bt.run_backtest(bars, events, cfg(sl={"value": 50.0}))
    assert len(res["trades"]) >= 2
    mc = bt.monte_carlo(res["trades"], 10000.0, paths=200, seed=42,
                        skip_pct=0.10)
    assert mc["paths"] == 200
    assert mc["p5_final"] <= mc["median_final"]
    assert mc["p95_max_dd"] >= 0.0
    assert mc["skip_variant"] is not None


def test_cost_stress_nets_fall_with_costs():
    bars = flat([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110])
    res = bt.cost_stress(
        bars, [ev(0, "BUY")],
        cfg(tp_legs=[{"rr": 2.0, "fraction": 1.0}], sl={"value": 5.0}))
    assert [r["cost_multiplier"] for r in res] == [1.0, 2.0, 3.0]
    assert res[2]["net"] <= res[0]["net"]


def test_ledger_csv_and_equity_consistency():
    bars = flat([100, 101, 102, 103, 104, 105, 104, 103, 102, 101,
                 100, 101, 102, 103, 104, 105])
    events = [ev(0, "BUY"), ev(5, "SELL"), ev(10, "BUY")]
    res = bt.run_backtest(bars, events, cfg(sl={"value": 50.0}))
    csv_text = bt.ledger_to_csv(res)
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",") == bt.LEDGER_COLUMNS
    assert len(lines) - 1 == len(res["legs"])
    final_eq = res["equity_curve"][-1]["equity"]
    assert abs(final_eq - (10000.0 + sum(t["net"] for t in res["trades"]))) < 0.01
    assert res["metrics"]["buy_hold_return_pct"] is not None


def test_config_validation():
    try:
        bt.validate_config({"direction": "sideways"})
    except ValueError:
        pass
    else:
        raise AssertionError("bad direction must raise")
    try:
        bt.validate_config({"tp_legs": [{"rr": 1.0, "fraction": 0.6},
                                        {"rr": 2.0, "fraction": 0.6}]})
    except ValueError:
        pass
    else:
        raise AssertionError("fractions summing > 1 must raise")


def test_same_side_signal_ignored():
    bars = flat([100, 101, 102, 103, 104, 105])
    res = bt.run_backtest(bars, [ev(0, "BUY"), ev(3, "BUY")],
                          cfg(sl={"value": 50.0}))
    assert len(res["trades"]) == 1
    assert res["warnings"]["ignored_signals"] >= 1


def test_activation_trail_stays_off_until_activation():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),    # fill 100.1, SL 95.1
            bar(120, 100.5, 106, 103, 104),  # peak 106, close profit 3.9 < 5R? no: 5.0
            bar(180, 104, 105, 100.5, 105),  # dip below would-be trail 101: survives
            bar(240, 105, 109, 104, 108),    # close profit 7.9 >= 5 -> activated, trail 104
            bar(300, 108, 108, 103, 104)]    # low 103 <= 104 -> trail exit
    res = bt.run_backtest(
        bars, [ev(0, "BUY")],
        cfg(trailing={"type": "fixed", "distance": 5.0,
                      "activation": 1.0, "activation_unit": "R"}))
    t = res["trades"][0]
    assert t["final_reason"] == "TrailingStop"
    assert t["exit_bar"] == 5
    assert abs(t["legs"][0]["price"] - 103.9) < 1e-9


def test_close_exit_bars_and_max_bars():
    bars = flat([100, 101, 102, 103, 104, 105, 106, 107])
    res = bt.run_backtest(bars, [ev(0, "BUY")],
                          cfg(sl={"value": 50.0}, close_exit_bars=3))
    t = res["trades"][0]
    assert t["final_reason"] == "CloseExit"
    assert t["exit_bar"] == 5  # held 3 bars at bar 4 close, next-open fill
    res2 = bt.run_backtest(bars, [ev(0, "BUY")],
                           cfg(sl={"value": 50.0}, max_bars=2))
    assert res2["trades"][0]["final_reason"] == "TimeExit"


def test_trailing_atr_and_pct_run():
    bars = [bar(0, 100, 101, 99, 100),
            bar(60, 100, 101, 99, 100.5),
            bar(120, 100.5, 110, 104, 109),
            bar(180, 109, 109, 106, 107),
            bar(240, 107, 108, 100, 101)]
    for tr in ({"type": "atr", "atr_length": 14, "atr_mult": 2.0},
               {"type": "pct", "distance": 2.0}):
        res = bt.run_backtest(bars, [ev(0, "BUY")], cfg(trailing=tr))
        assert len(res["trades"]) == 1
        assert res["trades"][0]["final_reason"] in (
            "TrailingStop", "StopLoss", "EndOfRange", "GapExit")


def test_playbook_preset():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 100.5),
            bar(120, 101, 116, 100, 115),
            bar(180, 115, 115, 96, 97),
            bar(240, 97, 98, 94, 95)]
    res = bt.run_backtest(bars, [ev(0, "BUY")], cfg(playbook=True))
    t = res["trades"][0]
    assert len(t["legs"]) == 2
    assert t["legs"][0]["reason"] == "TakeProfit1"
    assert abs(t["legs"][0]["qty"] - 0.07) < 1e-9  # 75% of 0.10
    assert res["snapshot"]["tp_legs"] == [{"rr": 3.0, "fraction": 0.75}]


def test_short_side_mirror():
    bars = [bar(0, 100, 100.5, 99.5, 100),
            bar(60, 100, 101, 99, 99.5),    # fill short 99.9 (open - slip)
            bar(120, 99.5, 106, 99, 100)]   # high 106 >= SL 104.9 -> stop
    res = bt.run_backtest(bars, [ev(0, "SELL")], cfg())
    t = res["trades"][0]
    assert t["side"] == "short"
    assert t["final_reason"] == "StopLoss"
    assert abs(t["legs"][0]["price"] - 105.0) < 1e-9  # SL 104.9 + slip
