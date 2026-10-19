"""Standard-indicator tests - unittest, no network.

Run: python3 -m unittest test_indicators -v (from app/)
Covers: textbook indicator formulas (hand-computed values, independent
of the module under test) + the 16-indicator Default Pack (factory
defaults, catalogue overlay flags, settings-API pure helpers).
The test suite covers only the standard indicators.
"""
import unittest


# ---------------------------------------------------------------------
def _ind_ctx(closes, highs=None, lows=None, start=1000, step=60):
    import pandas as pd
    n = len(closes)
    highs = highs if highs is not None else [c + 1.0 for c in closes]
    lows = lows if lows is not None else [c - 1.0 for c in closes]
    bars = pd.DataFrame({
        "time": [start + step * i for i in range(n)],
        "open": list(closes), "high": list(highs),
        "low": list(lows), "close": list(closes)})
    return {"symbol": "XAUUSD", "tf": "1m", "bars": bars, "inputs": {},
            "minutes": 1, "get_bars": lambda tf: bars}


class NewIndicatorFormulaTests(unittest.TestCase):
    def test_rsi_all_gains_is_100(self):
        from indicators import rsi as rsi_mod
        res = rsi_mod.compute(_ind_ctx([100.0 + i for i in range(16)]))
        self.assertEqual(res["values"]["rsi14"], 100.0)

    def test_rsi_flat_is_50(self):
        from indicators import rsi as rsi_mod
        res = rsi_mod.compute(_ind_ctx([100.0] * 20))
        self.assertEqual(res["values"]["rsi14"], 50.0)

    def test_atr_constant_range_is_2(self):
        from indicators import atr as atr_mod
        closes = [100.0] * 16
        res = atr_mod.compute(_ind_ctx(
            closes, highs=[101.0] * 16, lows=[99.0] * 16))
        self.assertAlmostEqual(res["values"]["atr14"], 2.0, places=9)

    def test_bollinger_arithmetic_series(self):
        import math
        import statistics
        from indicators import bollinger as bb_mod
        closes = [100.0 + i for i in range(20)]
        res = bb_mod.compute(_ind_ctx(closes))
        mid = sum(closes) / 20.0
        sd = statistics.stdev(closes)  # ddof=1, independent of pandas
        self.assertAlmostEqual(res["values"]["bb_mid"], mid, places=6)
        self.assertAlmostEqual(res["values"]["bb_up"], mid + 2 * sd,
                               places=6)
        self.assertAlmostEqual(res["values"]["bb_dn"], mid - 2 * sd,
                               places=6)

    def test_macd_constant_series_is_zero(self):
        from indicators import macd as macd_mod
        res = macd_mod.compute(_ind_ctx([100.0] * 60))
        self.assertAlmostEqual(res["values"]["macd"], 0.0, places=9)
        self.assertAlmostEqual(res["values"]["macd_signal"], 0.0, places=9)
        self.assertAlmostEqual(res["values"]["macd_hist"], 0.0, places=9)

    def test_macd_rising_series_positive(self):
        from indicators import macd as macd_mod
        res = macd_mod.compute(_ind_ctx([100.0 + i for i in range(60)]))
        self.assertGreater(res["values"]["macd"], 0.0)

    def test_sma_and_stochastic_and_adx_sanity(self):
        from indicators import sma as sma_mod
        from indicators import stochastic as stoch_mod
        from indicators import adx as adx_mod
        closes = [100.0 + i for i in range(60)]
        res = sma_mod.compute(_ind_ctx(closes, ))
        # SMA(50) of last 50 closes 110..159 -> mean 134.5
        res50 = sma_mod.compute({**_ind_ctx(closes), "inputs": {"length": 50}})
        self.assertAlmostEqual(res50["values"]["sma50"], 134.5, places=6)
        st = stoch_mod.compute(_ind_ctx(closes))
        self.assertTrue(0.0 <= st["values"]["stoch_k"] <= 100.0)
        self.assertTrue(0.0 <= st["values"]["stoch_d"] <= 100.0)
        ax = adx_mod.compute(_ind_ctx(closes))
        self.assertTrue(0.0 <= ax["values"]["adx14"] <= 100.0)
        # Pure uptrend: +DI above -DI.
        self.assertGreater(ax["values"]["plus_di"], ax["values"]["minus_di"])

    def test_new_indicators_no_sweeps_no_alerts(self):
        # Pure-value contract: new indicators never emit sweeps/alerts.
        from indicators import (rsi as rsi_mod, macd as macd_mod,
                                bollinger as bb_mod, adx as adx_mod,
                                stochastic as stoch_mod, atr as atr_mod,
                                sma as sma_mod)
        ctx = _ind_ctx([100.0 + i for i in range(60)])
        for mod in (rsi_mod, macd_mod, bb_mod, adx_mod, stoch_mod,
                    atr_mod, sma_mod):
            res = mod.compute(ctx)
            self.assertEqual(res["sweeps"], [], mod.META["id"])


class DefaultIndicatorsPackTests(unittest.TestCase):
    """Default Indicators Pack (full set of 16) + settings API.

    Formula tests use hand-computed values (independent of the module
    under test). Settings tests exercise the pure helpers the
    /api/indicator_settings endpoints wrap (server.py itself is not
    imported by this suite - same as the existing pattern).
    """

    def _ctx_vol(self, closes, vols, highs=None, lows=None, opens=None):
        import pandas as pd
        n = len(closes)
        highs = highs if highs is not None else [c + 1.0 for c in closes]
        lows = lows if lows is not None else [c - 1.0 for c in closes]
        opens = opens if opens is not None else list(closes)
        bars = pd.DataFrame({
            "time": [1000 + 60 * i for i in range(n)],
            "open": list(opens), "high": list(highs),
            "low": list(lows), "close": list(closes),
            "volume": list(vols)})
        return {"symbol": "XAUUSD", "tf": "1m", "bars": bars, "inputs": {},
                "minutes": 1, "get_bars": lambda tf: bars}

    def test_ema4_hand_computed(self):
        from indicators import ema4 as m
        # EMA(9) on 10..20: seed SMA(10..18)=14 at idx 8, then
        # 19*.2+14*.8=15.0, 20*.2+15*.8=16.0.
        res = m.compute(_ind_ctx([10.0 + i for i in range(11)]))
        self.assertAlmostEqual(res["values"]["ema9"], 16.0, places=9)
        self.assertEqual(res["sweeps"], [])
        # EMA(21) has no value on 11 bars - no key, no crash.
        self.assertNotIn("ema21", res["values"])

    def test_ema4_show_toggle(self):
        from indicators import ema4 as m
        ctx = _ind_ctx([100.0 + i for i in range(60)])
        ctx["inputs"] = {"show1": False, "show2": False,
                         "show3": False, "show4": False}
        res = m.compute(ctx)
        self.assertEqual(res["lines"], [])

    def test_volume_histogram(self):
        from indicators import volume as m
        # Bar 0 flat (close == open -> up), bar 1 zero volume -> skipped,
        # bar 2 down.
        res = m.compute(self._ctx_vol(
            [10.0, 11.0, 10.0], [5.0, 0.0, 7.0],
            opens=[10.0, 10.0, 11.0]))
        self.assertEqual(len(res["lines"]), 2)
        self.assertEqual(res["lines"][0]["color"], "#26A69A")
        self.assertEqual(res["lines"][1]["color"], "#EF5350")
        self.assertEqual(res["values"]["volume"], 7.0)
        self.assertEqual(res["sweeps"], [])

    def test_volume_no_data_honest(self):
        from indicators import volume as m
        res = m.compute(_ind_ctx([100.0] * 10))
        self.assertEqual(res["lines"], [])
        self.assertIn("No volume data", res["dashboard"]["rows"][0]["text"])

    def test_obv_hand_computed(self):
        from indicators import obv as m
        # OBV: 0, +20, -30 -> -10, +40 -> 30.
        res = m.compute(self._ctx_vol([100.0, 101.0, 100.0, 102.0],
                                      [10.0, 20.0, 30.0, 40.0]))
        self.assertAlmostEqual(res["values"]["obv"], 30.0, places=9)
        self.assertEqual(res["sweeps"], [])

    def test_cci_hand_computed(self):
        from indicators import cci as m
        ctx = _ind_ctx([10.0, 11.0, 12.0], highs=[10.0, 11.0, 12.0],
                       lows=[9.0, 10.0, 11.0])
        ctx["inputs"] = {"length": 3}
        res = m.compute(ctx)
        # TP 29/3, 32/3, 35/3; SMA 32/3; MD 2/3; CCI = 1/(0.015*2/3) = 100.
        self.assertAlmostEqual(res["values"]["cci3"], 100.0, places=6)
        self.assertEqual(res["sweeps"], [])

    def test_cci_flat_is_zero(self):
        from indicators import cci as m
        res = m.compute(_ind_ctx([100.0] * 25))
        self.assertAlmostEqual(res["values"]["cci20"], 0.0, places=9)

    def test_williams_hand_computed(self):
        from indicators import williams as m
        ctx = _ind_ctx([10.0, 10.5], highs=[10.0, 11.0], lows=[9.0, 10.0])
        ctx["inputs"] = {"length": 2}
        res = m.compute(ctx)
        # (11 - 10.5) / (11 - 9) * -100 = -25.
        self.assertAlmostEqual(res["values"]["williams_r2"], -25.0, places=9)
        self.assertEqual(res["sweeps"], [])

    def test_psar_hand_computed(self):
        from indicators import psar as m
        ctx = _ind_ctx([9.5, 10.5, 11.5, 11.0, 9.5],
                       highs=[10.0, 11.0, 12.0, 11.5, 10.0],
                       lows=[9.0, 10.0, 11.0, 10.5, 9.0])
        res = m.compute(ctx)
        # Hand walk (accel 0.02, max 0.2): SAR 9.0, 9.0, 9.0, 9.18,
        # then L=9 < 9.3492 flips the trend -> SAR = prior EP = 12.0.
        self.assertAlmostEqual(res["values"]["psar"], 12.0, places=9)
        self.assertEqual(res["values"]["psar_trend"], -1)
        self.assertEqual(len(res["boxes"]), 5)
        self.assertEqual(res["boxes"][0]["bgcolor"], "#26A69A")
        self.assertEqual(res["boxes"][-1]["bgcolor"], "#EF5350")
        self.assertEqual(res["sweeps"], [])

    def test_ichimoku_hand_computed(self):
        from indicators import ichimoku as m
        closes = [100.0 + i for i in range(60)]
        ctx = _ind_ctx(closes, highs=list(closes),
                       lows=[99.0 + i for i in range(60)])
        res = m.compute(ctx)
        # Tenkan (159+150)/2=154.5; Kijun (159+133)/2=146.0;
        # Senkou A 150.25; Senkou B (159+107)/2=133.0; Chikou 159.
        self.assertAlmostEqual(res["values"]["tenkan"], 154.5, places=9)
        self.assertAlmostEqual(res["values"]["kijun"], 146.0, places=9)
        self.assertAlmostEqual(res["values"]["senkou_a"], 150.25, places=9)
        self.assertAlmostEqual(res["values"]["senkou_b"], 133.0, places=9)
        self.assertAlmostEqual(res["values"]["chikou"], 159.0, places=9)
        self.assertTrue(len(res["areas"]) >= 1)  # cloud fill exists
        self.assertTrue(len(res["lines"]) > 0)
        self.assertEqual(res["sweeps"], [])

    def test_supertrend_hand_computed(self):
        from indicators import supertrend as m
        ctx = _ind_ctx([9.0, 10.0, 11.0, 8.0],
                       highs=[10.0, 11.0, 12.0, 9.0],
                       lows=[8.0, 9.0, 10.0, 7.0])
        ctx["inputs"] = {"length": 2, "mult": 1.0}
        res = m.compute(ctx)
        # ATR: TR 2,2,2,4 -> first ATR (2+2)/2=2 at idx 2; ATR[3]=3.
        # i=2: bands 13/9, trend up, ST=9. i=3: close 11 stays inside,
        # final lower = max(5, 9) = 9 -> ST=9, trend up.
        self.assertAlmostEqual(res["values"]["supertrend"], 9.0, places=9)
        self.assertEqual(res["values"]["supertrend_dir"], 1)
        self.assertEqual(res["sweeps"], [])

    def test_new_pack_no_sweeps(self):
        import indicators as indlib
        ctx = _ind_ctx([100.0 + i for i in range(60)])
        for ind_id in ("ema4", "volume", "obv", "cci", "williams", "psar",
                       "ichimoku", "supertrend"):
            mod = indlib.get_indicator(ind_id)
            self.assertIsNotNone(mod, ind_id)
            res = mod.compute(ctx)
            self.assertEqual(res["sweeps"], [], ind_id)
            self.assertNotIn("alert", str(res))

    def test_bollinger_fill_areas(self):
        from indicators import bollinger as m
        res = m.compute(_ind_ctx([100.0 + i for i in range(30)]))
        self.assertIn("areas", res)
        self.assertTrue(len(res["areas"]) >= 1)
        a = res["areas"][0]
        self.assertTrue(all(u > lo for u, lo in zip(a["upper"], a["lower"])))
        self.assertAlmostEqual(a["opacity"], 0.08, places=9)

    def test_macd_histogram_drawn(self):
        from indicators import macd as m
        res = m.compute(_ind_ctx([100.0 + i for i in range(60)]))
        hist_bars = [ln for ln in res["lines"]
                     if ln["t0"] == ln["t1"] and ln["p0"] == 0.0]
        self.assertTrue(len(hist_bars) > 0)
        self.assertIn("#26A69A", [ln["color"] for ln in hist_bars])

    def test_adx_draws_di_and_level(self):
        from indicators import adx as m
        res = m.compute(_ind_ctx([100.0 + i for i in range(60)]))
        colors = set(ln["color"] for ln in res["lines"])
        self.assertIn("#FF6D00", colors)   # ADX
        self.assertIn("#26A69A", colors)   # +DI
        self.assertIn("#EF5350", colors)   # DI
        self.assertIn("#787B86", colors)   # 25 level

    def test_pack_factory_defaults(self):
        import indicators as indlib
        want = {
            "ema4": {"length1": 9, "length2": 21, "length3": 50,
                     "length4": 200, "color1": "#2962FF",
                     "color2": "#FF6D00", "color3": "#00C853",
                     "color4": "#D50000", "width": 1},
            "sma": {"length": 20, "color": "#FFCA28", "width": 1},
            "rsi": {"length": 14, "color": "#7E57C2",
                    "band_color": "#787B86"},
            "macd": {"fast": 12, "slow": 26, "signal": 9,
                     "macd_color": "#2962FF", "signal_color": "#FF6D00",
                     "hist_up_color": "#26A69A",
                     "hist_down_color": "#EF5350"},
            "bollinger": {"length": 20, "mult": 2.0,
                          "mid_color": "#FF6D00", "band_color": "#2962FF"},
            "vwap": {"vwap_color": "#FF9800", "show_bands": True,
                     "show_band3": True},
            "atr": {"length": 14, "color": "#26A69A"},
            "stochastic": {"k_length": 14, "k_smooth": 3, "d_smooth": 3,
                           "overbought": 80.0, "oversold": 20.0},
            "adx": {"length": 14, "adx_color": "#FF6D00",
                    "plus_color": "#26A69A", "minus_color": "#EF5350",
                    "level": 25.0},
            "volume": {"up_color": "#26A69A", "down_color": "#EF5350"},
            "obv": {"color": "#2962FF"},
            "cci": {"length": 20, "color": "#7E57C2"},
            "williams": {"length": 14, "color": "#FF6D00"},
            "psar": {"accel": 0.02, "max_af": 0.2},
            "ichimoku": {"tenkan": 9, "kijun": 26, "senkou_b": 52,
                         "displacement": 26},
            "supertrend": {"length": 10, "mult": 3.0},
        }
        for ind_id, defaults in want.items():
            mod = indlib.get_indicator(ind_id)
            self.assertIsNotNone(mod, ind_id)
            got = indlib.default_inputs(mod)
            for k, v in defaults.items():
                self.assertEqual(got.get(k), v, "%s.%s" % (ind_id, k))

    def test_pack_catalogue_overlay_flags(self):
        import indicators as indlib
        inds, errs = indlib.list_indicators()
        self.assertEqual(errs, [])
        by_id = {i["id"]: i for i in inds}
        overlay = {"ema4", "sma", "bollinger", "vwap", "psar", "ichimoku",
                   "supertrend"}
        pane = {"rsi", "macd", "atr", "stochastic", "adx", "volume", "obv",
                "cci", "williams"}
        for ind_id in overlay:
            self.assertTrue(by_id[ind_id]["overlay"], ind_id)
        for ind_id in pane:
            self.assertFalse(by_id[ind_id]["overlay"], ind_id)

    def test_settings_api_save_roundtrip(self):
        import indicators as indlib
        saved = {}
        new_saved, payload, status = indlib.apply_indicator_settings(
            saved, "rsi",
            {"indicator": "rsi",
             "settings": {"length": 21, "color": "#112233"}})
        self.assertEqual(status, 200)
        self.assertEqual(new_saved["rsi"]["length"], 21)
        self.assertEqual(new_saved["rsi"]["color"], "#112233")
        self.assertEqual(saved, {})  # input never mutated
        merged = indlib.merge_indicator_settings(
            indlib.get_indicator("rsi"), new_saved["rsi"])
        self.assertEqual(merged["length"], 21)
        self.assertEqual(merged["color"], "#112233")
        # Untouched keys keep factory defaults.
        self.assertEqual(merged["overbought"], 70.0)

    def test_settings_api_clamps_and_rejects(self):
        import indicators as indlib
        # int clamped to max.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "rsi", {"settings": {"length": 500}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["length"], 100)
        # Unknown key rejected.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "rsi", {"settings": {"nope": 1}})
        self.assertEqual(status, 400)
        self.assertIn("unknown setting", payload["error"])
        # Unknown indicator rejected.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "not_a_real_indicator", {"settings": {"length": 14}})
        self.assertEqual(status, 400)
        # Bad color rejected.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "rsi", {"settings": {"color": "red"}})
        self.assertEqual(status, 400)
        # Bad options value rejected.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "rsi", {"settings": {"source": "Bogus"}})
        self.assertEqual(status, 400)
        # Bool must be a real bool.
        _, payload, status = indlib.apply_indicator_settings(
            {}, "rsi", {"settings": {"show_bands": 1}})
        self.assertEqual(status, 400)

    def test_settings_api_reset(self):
        import indicators as indlib
        saved = {"rsi": {"length": 21}, "macd": {"fast": 5}}
        new_saved, payload, status = indlib.apply_indicator_settings(
            saved, "rsi", {"indicator": "rsi", "reset": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reset"])
        self.assertNotIn("rsi", new_saved)
        self.assertIn("macd", new_saved)  # other indicators untouched
        # Merge with no overrides == factory defaults.
        merged = indlib.merge_indicator_settings(
            indlib.get_indicator("rsi"), {})
        self.assertEqual(merged,
                         indlib.default_inputs(indlib.get_indicator("rsi")))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
