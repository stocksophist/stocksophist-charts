"""Bollinger Bands - SMA(20) +/- mult * sigma (sample std, ddof=1).

Mid = SMA(length) of close. Upper = Mid + mult * sigma, Lower = Mid - mult *
sigma, where sigma is the sample standard deviation (ddof=1) of close over
the same window. Bandwidth = (Upper - Lower) / Mid.

Pure value - draws Mid/Upper/Lower, dashboard with latest values.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "bollinger",
    "name": "Bollinger Bands",
    "author": "StockSophist",
    "description": "Bollinger Bands (SMA +/- N sigma, sample std ddof=1) with a "
                   "light fill between the bands. Reference only, never "
                   "signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 20, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "mult", "label": "StdDev multiplier", "type": "float",
     "default": 2.0, "min": 0.5, "max": 5.0, "group": "Inputs"},
    {"key": "mid_color", "label": "Mid color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "band_color", "label": "Band color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
    {"key": "show_mid", "label": "Show middle line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_bands", "label": "Show upper / lower bands", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_fill", "label": "Fill between bands", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "fill_color", "label": "Fill color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "fill_opacity", "label": "Fill opacity", "type": "float",
     "default": 0.08, "min": 0.0, "max": 0.5, "group": "Style"},
]


def compute_bollinger_series(c, length, mult):
    """Returns (mid, upper, lower) lists, None until warmed up."""
    n = len(c)
    mid = [None] * n
    up = [None] * n
    dn = [None] * n
    if n < length or length < 2:
        return mid, up, dn
    for i in range(length - 1, n):
        window = c[i - length + 1:i + 1]
        m = sum(window) / length
        var = sum((x - m) ** 2 for x in window) / (length - 1)  # ddof=1
        sd = var ** 0.5
        mid[i] = m
        up[i] = m + mult * sd
        dn[i] = m - mult * sd
    return mid, up, dn


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(2, int(inp.get("length", 20)))
    mult = float(inp.get("mult", 2.0))
    c = bars["close"].astype(float).tolist()
    mid, up, dn = compute_bollinger_series(c, length, mult)
    times = bars["time"].astype(int).tolist()
    mc = inp.get("mid_color", "#FF6D00")
    bc = inp.get("band_color", "#2962FF")
    width = int(inp.get("width", 1))
    lines = []
    start = max(1, n - 2000)
    series = []
    if inp.get("show_mid", True):
        series.append((mid, mc))
    if inp.get("show_bands", True):
        series.append((up, bc))
        series.append((dn, bc))
    for series_vals, color in series:
        prev_t = prev_v = None
        for t, v in zip(times[start:], series_vals[start:]):
            if v is None:
                prev_t = prev_v = None
                continue
            t = int(t)
            v = float(v)
            if prev_t is not None:
                lines.append({"t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                              "color": color, "width": width, "style": "Solid",
                              "label": "", "label_size": "Small"})
            prev_t, prev_v = t, v
    # Light fill between the upper and lower bands (one polygon run per
    # contiguous stretch; gaps break the fill like they break the lines).
    areas = []
    if inp.get("show_fill", True):
        run_t, run_up, run_lo = [], [], []
        for t, u, d in zip(times[start:], up[start:], dn[start:]):
            if u is None or d is None:
                if len(run_t) > 1:
                    areas.append({"t": run_t, "upper": run_up,
                                  "lower": run_lo,
                                  "color": inp.get("fill_color", "#2962FF"),
                                  "opacity": float(
                                      inp.get("fill_opacity", 0.08))})
                run_t, run_up, run_lo = [], [], []
                continue
            run_t.append(int(t))
            run_up.append(float(u))
            run_lo.append(float(d))
        if len(run_t) > 1:
            areas.append({"t": run_t, "upper": run_up, "lower": run_lo,
                          "color": inp.get("fill_color", "#2962FF"),
                          "opacity": float(inp.get("fill_opacity", 0.08))})
    last_mid = next((v for v in reversed(mid) if v is not None), None)
    last_up = next((v for v in reversed(up) if v is not None), None)
    last_dn = next((v for v in reversed(dn) if v is not None), None)
    bw = None
    if last_mid and last_up is not None and last_dn is not None and last_mid != 0:
        bw = (last_up - last_dn) / abs(last_mid)
    values = {"bb_mid": last_mid, "bb_up": last_up, "bb_dn": last_dn,
              "bb_width": bw}
    dash = None
    if last_mid is not None:
        dash = {"title": "Bollinger (%d, %.1f)" % (length, mult),
                "rows": [{"text": "Mid: %.2f | Up: %.2f | Dn: %.2f" % (
                    last_mid, last_up, last_dn), "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "areas": areas, "dashboard": dash,
            "sweeps": [], "values": values}
