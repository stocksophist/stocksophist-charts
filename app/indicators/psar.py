"""Parabolic SAR (Wilder / Welles).

Trend starts long. SAR starts at the first bar's low, EP (extreme
point) at the first bar's high, AF (acceleration) at `accel`.
Each bar: SAR += AF * (EP - SAR); in a long trend SAR is capped at the
lowest of the previous two lows (in a short trend, at the highest of
the previous two highs). A close through the SAR flips the trend: the
new SAR is the prior EP, EP resets to the reversal bar's extreme, and
AF resets to `accel`. Each new extreme steps AF up by `accel`, capped
at `max_af`.

Dots are drawn as small filled boxes (the line renderer only draws
segments) - one per bar, sized from the bar's own range so they stay
dot-like at any zoom. Up-trend dots sit below price, down-trend dots
above price.

Overlay. Pure value - draws dots, never signals or alerts.
Closed-bar-only.
"""

META = {
    "id": "psar",
    "name": "Parabolic SAR",
    "author": "StockSophist",
    "description": "Parabolic Stop and Reverse (Wilder). Dots trail price and "
                   "flip on a close through the SAR. "
                   "Reference only - never signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "accel", "label": "Acceleration", "type": "float",
     "default": 0.02, "min": 0.001, "max": 0.5, "group": "Inputs"},
    {"key": "max_af", "label": "Max acceleration", "type": "float",
     "default": 0.2, "min": 0.01, "max": 1.0, "group": "Inputs"},
    {"key": "up_color", "label": "Up-trend dot color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "down_color", "label": "Down-trend dot color", "type": "color",
     "default": "#EF5350", "group": "Style"},
]


def compute_psar_series(h, l, accel, max_af):
    """Returns (sar, trend) lists; trend True = up (long)."""
    n = len(h)
    sar = [None] * n
    trend = [True] * n
    if n == 0:
        return sar, trend
    tr = True
    s = l[0]
    ep = h[0]
    af = accel
    sar[0] = s
    trend[0] = True
    for i in range(1, n):
        s = s + af * (ep - s)
        if tr:
            # Long: SAR may not exceed the lowest of the prior two lows.
            cap = l[i - 1] if i < 2 else min(l[i - 1], l[i - 2])
            if s > cap:
                s = cap
            if l[i] < s:
                # Reversal: SAR becomes the prior extreme point.
                tr = False
                s = ep
                ep = l[i]
                af = accel
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(af + accel, max_af)
        else:
            # Short: SAR may not fall below the highest of the prior two highs.
            cap = h[i - 1] if i < 2 else max(h[i - 1], h[i - 2])
            if s < cap:
                s = cap
            if h[i] > s:
                tr = True
                s = ep
                ep = h[i]
                af = accel
            else:
                if l[i] < ep:
                    ep = l[i]
                    af = min(af + accel, max_af)
        sar[i] = s
        trend[i] = tr
    return sar, trend


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    accel = float(inp.get("accel", 0.02))
    max_af = float(inp.get("max_af", 0.2))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    sar, trend = compute_psar_series(h, l, accel, max_af)
    times = bars["time"].astype(int).tolist()
    minutes = ctx.get("minutes") or 1
    half = max(1, int(minutes * 30))  # half a bar each side of the dot
    up_c = inp.get("up_color", "#26A69A")
    dn_c = inp.get("down_color", "#EF5350")
    boxes = []
    start = max(0, n - 2000)
    for i in range(start, n):
        s = sar[i]
        if s is None:
            continue
        rng = h[i] - l[i]
        eps = max(rng * 0.20, abs(s) * 0.0005, 1e-9)
        t = int(times[i])
        boxes.append({"t0": t - half, "t1": t + half,
                      "p0": float(s) - eps, "p1": float(s) + eps,
                      "bgcolor": up_c if trend[i] else dn_c,
                      "border": "#00000000", "border_width": 0,
                      "label": "", "label_size": "Small"})
    last = float(sar[-1]) if sar and sar[-1] is not None else None
    last_trend = trend[-1] if trend else True
    dash = None
    if last is not None:
        dash = {"title": "Parabolic SAR (%.2f, %.2f)" % (accel, max_af),
                "rows": [{"text": "SAR: %.2f (%s)" % (
                    last, "Up" if last_trend else "Down"),
                    "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": [], "boxes": boxes, "dashboard": dash, "sweeps": [],
            "values": {"psar": last,
                       "psar_trend": 1 if last_trend else -1}}
