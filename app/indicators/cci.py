"""CCI - Commodity Channel Index (Lambert, 20).

Typical price TP = (High + Low + Close) / 3.
CCI = (TP - SMA(TP, length)) / (0.015 * mean deviation),
mean deviation = mean(|TP - SMA(TP, length)|) over the window.
Zero mean deviation (perfectly flat window) -> CCI = 0.
+100 / -100 reference levels mark the usual overbought/oversold zones.

Separate pane. Pure value - draws a line + levels, never signals or
alerts. Closed-bar-only.
"""

META = {
    "id": "cci",
    "name": "CCI",
    "author": "StockSophist",
    "description": "Commodity Channel Index (Lambert). +100 / -100 levels. "
                   "Reference only - never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 20, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#7E57C2", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "level_color", "label": "Level color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "show_levels", "label": "Show +100 / -100 levels", "type": "bool",
     "default": True, "group": "Style"},
]


def compute_cci_series(h, l, c, length):
    """CCI per Lambert. Returns list, None until warmed up."""
    n = len(c)
    out = [None] * n
    if n < length or length < 2:
        return out
    tp = [(hi + lo + cc) / 3.0 for hi, lo, cc in zip(h, l, c)]
    for i in range(length - 1, n):
        window = tp[i - length + 1:i + 1]
        sma = sum(window) / length
        md = sum(abs(x - sma) for x in window) / length
        out[i] = 0.0 if md == 0 else (tp[i] - sma) / (0.015 * md)
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(2, int(inp.get("length", 20)))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    cci = compute_cci_series(h, l, c, length)
    times = bars["time"].astype(int).tolist()
    color = inp.get("color", "#7E57C2")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], cci[start:]):
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
    if inp.get("show_levels", True) and n:
        t0, t1 = int(times[0]), int(times[-1])
        lc = inp.get("level_color", "#787B86")
        for lvl in (100.0, -100.0):
            lines.append({"t0": t0, "t1": t1, "p0": lvl, "p1": lvl,
                          "color": lc, "width": 1, "style": "Dashed",
                          "label": "", "label_size": "Small"})
    last = next((v for v in reversed(cci) if v is not None), None)
    values = {"cci%d" % length: (None if last is None else float(last))}
    dash = None
    if last is not None:
        dash = {"title": "CCI (%d)" % length,
                "rows": [{"text": "CCI: %.1f" % last, "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
