"""RSI - Relative Strength Index (Wilder, 14).

Standard Wilder RSI: first value = simple average of the first `length`
gains/losses, then Wilder smoothing (avg = (prev*(n-1) + current)/n).
RSI = 100 - 100 / (1 + RS), RS = avg_gain / avg_loss.
Divide-by-zero: avg_loss == 0 with avg_gain > 0 -> RSI = 100; both zero
(flat market) -> RSI = 50 (neutral, no movement).

Pure value - draws the RSI line + 70/30 levels, dashboard with the latest
value. Never signals or alerts. Closed-bar-only (the engine evaluates
indicators on closed bars).
"""

META = {
    "id": "rsi",
    "name": "RSI",
    "author": "StockSophist",
    "description": "Relative Strength Index (Wilder). 70/30 overbought/oversold "
                   "levels. Reference only - never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 14, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "source", "label": "Source", "type": "options",
     "options": ["Close", "Open", "High", "Low", "HL/2", "HLC/3", "OHLC/4"],
     "default": "Close", "group": "Inputs"},
    {"key": "overbought", "label": "Overbought level", "type": "float",
     "default": 70.0, "min": 50, "max": 95, "group": "Inputs"},
    {"key": "oversold", "label": "Oversold level", "type": "float",
     "default": 30.0, "min": 5, "max": 50, "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#7E57C2", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "band_color", "label": "Level color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "show_bands", "label": "Show overbought / oversold levels",
     "type": "bool", "default": True, "group": "Style"},
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


def compute_rsi_series(src, length):
    """Wilder RSI for a price series. Returns list, None until warmed up."""
    n = len(src)
    out = [None] * n
    if n <= length:
        return out
    gains, losses = [], []
    for i in range(1, length + 1):
        ch = src[i] - src[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    out[length] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(length + 1, n):
        ch = src[i] - src[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(ch, 0.0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-ch, 0.0)) / length
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(2, int(inp.get("length", 14)))
    src = _source(bars, inp.get("source", "Close"))
    rsi = compute_rsi_series(src, length)
    times = bars["time"].astype(int).tolist()
    ob = float(inp.get("overbought", 70.0))
    os_ = float(inp.get("oversold", 30.0))
    color = inp.get("color", "#7E57C2")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], rsi[start:]):
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
    # 70/30 reference levels (full-width horizontal segments).
    if n and inp.get("show_bands", True):
        t0, t1 = int(times[0]), int(times[-1])
        lc = inp.get("band_color", "#787B86")
        for lvl in (ob, os_):
            lines.append({"t0": t0, "t1": t1, "p0": lvl, "p1": lvl,
                          "color": lc, "width": 1, "style": "Dashed",
                          "label": "", "label_size": "Small"})
    last = next((v for v in reversed(rsi) if v is not None), None)
    values = {"rsi%d" % length: (None if last is None else float(last))}
    dash = None
    if last is not None:
        zone = "Overbought" if last >= ob else ("Oversold" if last <= os_ else "Neutral")
        dash = {"title": "RSI (%d)" % length,
                "rows": [{"text": "RSI: %.1f (%s)" % (last, zone),
                          "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
