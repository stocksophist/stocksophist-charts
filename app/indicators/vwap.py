"""VWAP - Volume-Weighted Average Price with standard-deviation bands.

Anchored to the start of the day or week (UTC): cumulative
sum(typical_price * volume) / sum(volume), resetting at each anchor.
Bands are VWAP +/- mult * sigma, where sigma is the volume-weighted
standard deviation of the source price inside the anchor period.

Volume is MT5 tick volume (the number of price updates per bar - spot
gold has no central exchange, so this is the standard activity proxy).
Bars with no volume data are weighted equally so the line still works
on older/demo history.

Reference only - draws lines, never signals or alerts.
"""

from datetime import datetime, timezone

META = {
    "id": "vwap",
    "name": "VWAP",
    "author": "StockSophist",
    "description": "Volume-Weighted Average Price anchored to the day/week "
                   "(UTC) with optional standard-deviation bands. Uses MT5 "
                   "tick volume; bars without volume are weighted equally.",
    "overlay": True,
}

INPUTS = [
    {"key": "source", "label": "Source", "type": "options",
     "options": ["Typical (HLC/3)", "Close", "HL/2", "OHLC/4"],
     "default": "Typical (HLC/3)", "group": "Inputs"},
    {"key": "anchor", "label": "Anchor period", "type": "options",
     "options": ["Day", "Week"], "default": "Day", "group": "Inputs"},

    {"key": "show_vwap", "label": "Show VWAP line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "vwap_color", "label": "Line color", "type": "color",
     "default": "#FF9800", "group": "Style"},
    {"key": "vwap_width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "vwap_style", "label": "Line style", "type": "options",
     "options": ["Solid", "Dashed", "Dotted"], "default": "Solid",
     "group": "Style"},

    {"key": "show_bands", "label": "Show bands", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_band2", "label": "Show band 2", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_band3", "label": "Show band 3", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "band1_mult", "label": "Band 1 (x sigma)", "type": "float",
     "default": 1.0, "min": 0.1, "max": 5.0, "group": "Style"},
    {"key": "band2_mult", "label": "Band 2 (x sigma)", "type": "float",
     "default": 2.0, "min": 0.1, "max": 5.0, "group": "Style"},
    {"key": "band3_mult", "label": "Band 3 (x sigma)", "type": "float",
     "default": 3.0, "min": 0.1, "max": 5.0, "group": "Style"},
    {"key": "band_color", "label": "Band color", "type": "color",
     "default": "#9e9e9e", "group": "Style"},
    {"key": "band_width", "label": "Band width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
    {"key": "band_style", "label": "Band style", "type": "options",
     "options": ["Solid", "Dashed", "Dotted"], "default": "Dashed",
     "group": "Style"},
]


def _anchor_key(ts, anchor):
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    if anchor == "Week":
        iso = dt.isocalendar()
        return (iso[0], iso[1])
    return (dt.year, dt.month, dt.day)


def compute(ctx):
    bars = ctx["bars"]      # DataFrame: time, open, high, low, close [, volume]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": []}

    times = bars["time"].astype(int).tolist()
    o = bars["open"].astype(float).tolist()
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    if "volume" in bars.columns:
        vol = bars["volume"].fillna(0).astype(float).tolist()
    else:
        vol = [0.0] * n

    src_name = inp.get("source", "Typical (HLC/3)")
    if src_name == "Close":
        src = c
    elif src_name == "HL/2":
        src = [(hi + lo) / 2.0 for hi, lo in zip(h, l)]
    elif src_name == "OHLC/4":
        src = [(oo + hi + lo + cc) / 4.0 for oo, hi, lo, cc in zip(o, h, l, c)]
    else:  # Typical (HLC/3)
        src = [(hi + lo + cc) / 3.0 for hi, lo, cc in zip(h, l, c)]

    anchor = inp.get("anchor", "Day")
    vw, up1, dn1, up2, dn2, up3, dn3 = ([] for _ in range(7))
    cum_pv = cum_v = cum_pv2 = 0.0
    cur_key = None
    for i in range(n):
        k = _anchor_key(times[i], anchor)
        if k != cur_key:
            cur_key = k
            cum_pv = cum_v = cum_pv2 = 0.0
        p = src[i]
        w = vol[i] if vol[i] > 0 else 1.0  # no volume data -> equal weight
        cum_pv += p * w
        cum_v += w
        cum_pv2 += p * p * w
        vwap = cum_pv / cum_v
        var = cum_pv2 / cum_v - vwap * vwap
        sd = var ** 0.5 if var > 0 else 0.0
        vw.append(vwap)
        m1 = float(inp.get("band1_mult", 1.0))
        m2 = float(inp.get("band2_mult", 2.0))
        m3 = float(inp.get("band3_mult", 3.0))
        up1.append(vwap + m1 * sd); dn1.append(vwap - m1 * sd)
        up2.append(vwap + m2 * sd); dn2.append(vwap - m2 * sd)
        up3.append(vwap + m3 * sd); dn3.append(vwap - m3 * sd)

    # draw the last 2000 bars as segments (cheap and smooth)
    start = max(1, n - 2000)
    series = []
    if inp.get("show_vwap", True):
        series.append((vw, inp.get("vwap_color", "#FF9800"),
                       int(inp.get("vwap_width", 2)),
                       inp.get("vwap_style", "Solid")))
    if inp.get("show_bands", True):
        bc = inp.get("band_color", "#9e9e9e")
        bw = int(inp.get("band_width", 1))
        bs = inp.get("band_style", "Dashed")
        series.append((up1, bc, bw, bs))
        series.append((dn1, bc, bw, bs))
        if inp.get("show_band2", True):
            series.append((up2, bc, bw, bs))
            series.append((dn2, bc, bw, bs))
        if inp.get("show_band3", True):
            series.append((up3, bc, bw, bs))
            series.append((dn3, bc, bw, bs))

    lines = []
    for vals, color, width, style in series:
        prev_t, prev_v = None, None
        for t, v in zip(times[start:], vals[start:]):
            t = int(t)
            v = float(v)
            if prev_t is not None:
                lines.append({
                    "t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                    "color": color, "width": width, "style": style,
                    "label": "", "label_size": "Small",
                })
            prev_t, prev_v = t, v
    # latest numeric values for the AI snapshot (flat dict).
    values = {"vwap": float(vw[-1]), "vwap_up1": float(up1[-1]),
              "vwap_dn1": float(dn1[-1]), "vwap_up2": float(up2[-1]),
              "vwap_dn2": float(dn2[-1]), "vwap_up3": float(up3[-1]),
              "vwap_dn3": float(dn3[-1])} if n else {}
    return {"lines": lines, "dashboard": None, "sweeps": [], "values": values}
