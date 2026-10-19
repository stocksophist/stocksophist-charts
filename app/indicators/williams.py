"""Williams %R (Williams, 14).

%R = (HighestHigh(length) - Close) / (HighestHigh(length) -
LowestLow(length)) * -100. Ranges 0 (at the high) to -100 (at the low);
-20 / -80 reference levels mark the usual overbought/oversold zones.
Zero range (flat window) -> %R = -50 (midpoint, no movement).

Separate pane. Pure value - draws a line + levels, never signals or
alerts. Closed-bar-only.
"""

META = {
    "id": "williams",
    "name": "Williams %R",
    "author": "StockSophist",
    "description": "Williams %R momentum oscillator. -20 / -80 levels. "
                   "Reference only - never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 14, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "level_color", "label": "Level color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "show_levels", "label": "Show -20 / -80 levels", "type": "bool",
     "default": True, "group": "Style"},
]


def compute_williams_series(h, l, c, length):
    """Williams %R. Returns list, None until warmed up."""
    n = len(c)
    out = [None] * n
    if n < length or length < 2:
        return out
    for i in range(length - 1, n):
        hi = max(h[i - length + 1:i + 1])
        lo = min(l[i - length + 1:i + 1])
        rng = hi - lo
        out[i] = -50.0 if rng == 0 else (hi - c[i]) / rng * -100.0
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(2, int(inp.get("length", 14)))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    wr = compute_williams_series(h, l, c, length)
    times = bars["time"].astype(int).tolist()
    color = inp.get("color", "#FF6D00")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], wr[start:]):
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
        for lvl in (-20.0, -80.0):
            lines.append({"t0": t0, "t1": t1, "p0": lvl, "p1": lvl,
                          "color": lc, "width": 1, "style": "Dashed",
                          "label": "", "label_size": "Small"})
    last = next((v for v in reversed(wr) if v is not None), None)
    values = {"williams_r%d" % length: (None if last is None else float(last))}
    dash = None
    if last is not None:
        dash = {"title": "Williams %%R (%d)" % length,
                "rows": [{"text": "%%R: %.1f" % last, "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
