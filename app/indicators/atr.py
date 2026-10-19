"""ATR - Average True Range (Wilder, 14).

TR = max(H - L, |H - prevC|, |L - prevC|).
First ATR = SMA of the first `length` TRs, then Wilder smoothing:
ATR = (prev * (n-1) + TR) / n.

Pure value - draws the ATR line, dashboard with the latest value.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "atr",
    "name": "ATR",
    "author": "StockSophist",
    "description": "Average True Range (Wilder). Volatility measure - the "
                   "typical bar range. Reference only, never signals.",
    "overlay": False,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 14, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
]


def compute_tr_series(h, l, c):
    """True Range per bar (first bar: H - L)."""
    tr = []
    for i in range(len(h)):
        if i == 0:
            tr.append(h[0] - l[0])
        else:
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]),
                          abs(l[i] - c[i - 1])))
    return tr


def compute_atr_series(h, l, c, length):
    """Wilder ATR. Returns list, None until warmed up."""
    n = len(h)
    out = [None] * n
    if n <= length:
        return out
    tr = compute_tr_series(h, l, c)
    # First ATR = SMA of TR[1..length] (TR[0] has no prev close).
    out[length] = sum(tr[1:length + 1]) / length
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
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
    atr = compute_atr_series(h, l, c, length)
    times = bars["time"].astype(int).tolist()
    color = inp.get("color", "#26A69A")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], atr[start:]):
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
    last = next((v for v in reversed(atr) if v is not None), None)
    values = {"atr%d" % length: (None if last is None else float(last))}
    dash = None
    if last is not None:
        dash = {"title": "ATR (%d)" % length,
                "rows": [{"text": "ATR: %.2f" % last, "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
