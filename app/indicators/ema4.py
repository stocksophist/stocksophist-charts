"""EMA x4 - four exponential moving averages in one indicator.

Standard EMA: k = 2/(n+1), seeded with the SMA of the first n closes,
then EMA[i] = close[i]*k + EMA[i-1]*(1-k) (same seeding as macd.py's
compute_ema_series). Four independently configurable lengths, colors
and show toggles; one shared width / line style.

Overlay - draws on the price chart. Pure value - draws lines, never
signals or alerts. Closed-bar-only (the engine evaluates indicators on
closed bars).
"""

META = {
    "id": "ema4",
    "name": "EMA x4",
    "author": "StockSophist",
    "description": "Four EMAs in one indicator (9 / 21 / 50 / 200). "
                   "Reference only - never signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "length1", "label": "EMA 1 length", "type": "int",
     "default": 9, "min": 1, "max": 500, "group": "Inputs"},
    {"key": "length2", "label": "EMA 2 length", "type": "int",
     "default": 21, "min": 1, "max": 500, "group": "Inputs"},
    {"key": "length3", "label": "EMA 3 length", "type": "int",
     "default": 50, "min": 1, "max": 500, "group": "Inputs"},
    {"key": "length4", "label": "EMA 4 length", "type": "int",
     "default": 200, "min": 1, "max": 500, "group": "Inputs"},
    {"key": "source", "label": "Source", "type": "options",
     "options": ["Close", "Open", "High", "Low", "HL/2", "HLC/3", "OHLC/4"],
     "default": "Close", "group": "Inputs"},

    {"key": "color1", "label": "EMA 1 color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "color2", "label": "EMA 2 color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "color3", "label": "EMA 3 color", "type": "color",
     "default": "#00C853", "group": "Style"},
    {"key": "color4", "label": "EMA 4 color", "type": "color",
     "default": "#D50000", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
    {"key": "style", "label": "Line style", "type": "options",
     "options": ["Solid", "Dashed", "Dotted"], "default": "Solid",
     "group": "Style"},
    {"key": "show1", "label": "Show EMA 1", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show2", "label": "Show EMA 2", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show3", "label": "Show EMA 3", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show4", "label": "Show EMA 4", "type": "bool",
     "default": True, "group": "Style"},
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


def compute_ema_series(src, length):
    """EMA with k = 2/(n+1), seeded with SMA of first n values."""
    out = [None] * len(src)
    if len(src) < length or length < 1:
        return out
    k = 2.0 / (length + 1)
    ema = sum(src[:length]) / length
    out[length - 1] = ema
    for i in range(length, len(src)):
        ema = src[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    src = _source(bars, inp.get("source", "Close"))
    times = bars["time"].astype(int).tolist()
    width = int(inp.get("width", 1))
    style = inp.get("style", "Solid")
    lines = []
    values = {}
    start = max(1, n - 2000)
    # Per-line factory defaults - compute() fallbacks must match INPUTS,
    # because the engine calls compute with "inputs": {} (no defaults
    # applied yet) and a wrong fallback here would silently change the
    # indicator (all four EMAs would become length 50).
    _DEFS = ((9, "#2962FF"), (21, "#FF6D00"), (50, "#00C853"),
             (200, "#D50000"))
    for idx in (1, 2, 3, 4):
        if not inp.get("show%d" % idx, True):
            continue
        d_len, d_col = _DEFS[idx - 1]
        length = max(1, int(inp.get("length%d" % idx, d_len)))
        color = inp.get("color%d" % idx, d_col)
        ema = compute_ema_series(src, length)
        prev_t = prev_v = None
        for t, v in zip(times[start:], ema[start:]):
            if v is None:
                prev_t = prev_v = None
                continue
            t = int(t)
            v = float(v)
            if prev_t is not None:
                lines.append({"t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                              "color": color, "width": width, "style": style,
                              "label": "", "label_size": "Small"})
            prev_t, prev_v = t, v
        last = next((v for v in reversed(ema) if v is not None), None)
        if last is not None:
            values["ema%d" % length] = float(last)
    dash = None
    if values:
        rows = [{"text": "EMA: " + " | ".join(
            "EMA%d: %.2f" % (int(inp.get("length%d" % i,
                                         _DEFS[i - 1][0])),
                             values.get("ema%d" % int(inp.get(
                                 "length%d" % i, _DEFS[i - 1][0])), 0.0))
            for i in (1, 2, 3, 4)
            if "ema%d" % int(inp.get("length%d" % i,
                                     _DEFS[i - 1][0])) in values),
            "color": "#ffffff"}]
        dash = {"title": "EMA x4",
                "rows": rows,
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
