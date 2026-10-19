"""Supertrend (Kaufman / Olivier Seban).

Basic bands from the bar midpoint and Wilder ATR (same ATR convention
as atr.py: first ATR = SMA of TR[1..length]):
  basic upper = (High + Low) / 2 + mult * ATR
  basic lower = (High + Low) / 2 - mult * ATR
Trend flips when the previous close crosses the previous final band;
while in an uptrend the final lower band only ever rises (max of basic
lower and prior final lower), and while in a downtrend the final upper
band only ever falls (min of basic upper and prior final upper).
The Supertrend line follows the final band of the current trend.

Overlay - one line, colored by trend. Pure value - draws a line,
never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "supertrend",
    "name": "Supertrend",
    "author": "StockSophist",
    "description": "Supertrend (ATR bands that flip on a close through the "
                   "band). Reference only - never signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "length", "label": "ATR length", "type": "int",
     "default": 10, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "mult", "label": "Multiplier", "type": "float",
     "default": 3.0, "min": 0.5, "max": 10.0, "group": "Inputs"},
    {"key": "up_color", "label": "Up-trend color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "down_color", "label": "Down-trend color", "type": "color",
     "default": "#EF5350", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
]


def _atr_series(h, l, c, length):
    """Wilder ATR, same convention as atr.py (first ATR = SMA of
    TR[1..length], defined at index `length`). None until warmed up."""
    n = len(h)
    out = [None] * n
    if n <= length:
        return out
    tr = []
    for i in range(n):
        if i == 0:
            tr.append(h[0] - l[0])
        else:
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]),
                          abs(l[i] - c[i - 1])))
    out[length] = sum(tr[1:length + 1]) / length
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def compute_supertrend_series(h, l, c, length, mult):
    """Returns (st, trend) lists; trend True = up. None until ATR warms."""
    n = len(h)
    st = [None] * n
    trend = [True] * n
    atr = _atr_series(h, l, c, length)
    first = next((i for i, v in enumerate(atr) if v is not None), None)
    if first is None:
        return st, trend
    f_up = f_dn = None
    tr = True
    for i in range(first, n):
        hl2 = (h[i] + l[i]) / 2.0
        b_up = hl2 + mult * atr[i]
        b_dn = hl2 - mult * atr[i]
        if i == first:
            f_up, f_dn = b_up, b_dn
        else:
            if c[i - 1] > f_up:
                tr = True
            elif c[i - 1] < f_dn:
                tr = False
            if tr:
                f_dn = max(b_dn, f_dn)
                f_up = b_up
            else:
                f_up = min(b_up, f_up)
                f_dn = b_dn
        st[i] = f_dn if tr else f_up
        trend[i] = tr
    return st, trend


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    length = max(2, int(inp.get("length", 10)))
    mult = float(inp.get("mult", 3.0))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    st, trend = compute_supertrend_series(h, l, c, length, mult)
    times = bars["time"].astype(int).tolist()
    up_c = inp.get("up_color", "#26A69A")
    dn_c = inp.get("down_color", "#EF5350")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = prev_tr = None
    for t, v, tr in zip(times[start:], st[start:], trend[start:]):
        if v is None:
            prev_t = prev_v = prev_tr = None
            continue
        t = int(t)
        v = float(v)
        if prev_t is not None:
            # A trend flip breaks the line (no diagonal across the flip).
            if tr == prev_tr:
                lines.append({"t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                              "color": up_c if tr else dn_c,
                              "width": width, "style": "Solid",
                              "label": "", "label_size": "Small"})
        prev_t, prev_v, prev_tr = t, v, tr
    last = next((v for v in reversed(st) if v is not None), None)
    last_tr = trend[-1] if trend else True
    values = {"supertrend": (None if last is None else float(last)),
              "supertrend_dir": 1 if last_tr else -1}
    dash = None
    if last is not None:
        dash = {"title": "Supertrend (%d, %.1f)" % (length, mult),
                "rows": [{"text": "Supertrend: %.2f (%s)" % (
                    last, "Up" if last_tr else "Down"), "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
