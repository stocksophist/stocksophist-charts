"""SMA - Simple Moving Average.

Pure value - draws the SMA line, dashboard with the latest value.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "sma",
    "name": "SMA",
    "author": "StockSophist",
    "description": "Simple Moving Average of the source price. Reference only, "
                   "never signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 20, "min": 1, "max": 500, "group": "Inputs"},
    {"key": "source", "label": "Source", "type": "options",
     "options": ["Close", "Open", "High", "Low", "HL/2", "HLC/3", "OHLC/4"],
     "default": "Close", "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#FFCA28", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
]


def _source(bars, name):
    o = bars["open"].astype(float).tolist()
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    if name == "Open":
        return o
    if name == "High":
        return h
    if name == "Low":
        return l
    if name == "HL/2":
        return [(hi + lo) / 2.0 for hi, lo in zip(h, l)]
    if name == "HLC/3":
        return [(hi + lo + cc) / 3.0 for hi, lo, cc in zip(h, l, c)]
    if name == "OHLC/4":
        return [(oo + hi + lo + cc) / 4.0 for oo, hi, lo, cc in zip(o, h, l, c)]
    return c


def compute_sma_series(src, length):
    """SMA. Returns list, None until warmed up."""
    out = [None] * len(src)
    if len(src) < length or length < 1:
        return out
    window = sum(src[:length])
    out[length - 1] = window / length
    for i in range(length, len(src)):
        window += src[i] - src[i - length]
        out[i] = window / length
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(1, int(inp.get("length", 20)))
    src = _source(bars, inp.get("source", "Close"))
    sma = compute_sma_series(src, length)
    times = bars["time"].astype(int).tolist()
    color = inp.get("color", "#FFCA28")
    width = int(inp.get("width", 1))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], sma[start:]):
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
    last = next((v for v in reversed(sma) if v is not None), None)
    values = {"sma%d" % length: (None if last is None else float(last))}
    dash = None
    if last is not None:
        dash = {"title": "SMA (%d)" % length,
                "rows": [{"text": "SMA: %.2f" % last, "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
