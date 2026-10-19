"""Stochastic Oscillator (14, 3, 3).

%K = 100 * (C - lowest low 14) / (highest high 14 - lowest low 14)
(50 when the range is 0). %D = SMA(3) of %K. Slow %D = SMA(3) of %D.

Pure value - draws %K + Slow %D lines, dashboard with latest values.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "stochastic",
    "name": "Stochastic",
    "author": "StockSophist",
    "overlay": False,
    "description": "Stochastic Oscillator (14, 3, 3): %K, %D, Slow %D. "
                   "Reference only, never signals or alerts.",
}

INPUTS = [
    {"key": "k_length", "label": "%K length", "type": "int",
     "default": 14, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "k_smooth", "label": "%D smoothing", "type": "int",
     "default": 3, "min": 1, "max": 20, "group": "Inputs"},
    {"key": "d_smooth", "label": "Slow %D smoothing", "type": "int",
     "default": 3, "min": 1, "max": 20, "group": "Inputs"},
    {"key": "overbought", "label": "Overbought level", "type": "float",
     "default": 80.0, "min": 50, "max": 95, "group": "Inputs"},
    {"key": "oversold", "label": "Oversold level", "type": "float",
     "default": 20.0, "min": 5, "max": 50, "group": "Inputs"},
    {"key": "k_color", "label": "%K color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "d_color", "label": "Slow %D color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "band_color", "label": "Level color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "show_bands", "label": "Show overbought / oversold levels",
     "type": "bool", "default": True, "group": "Style"},
]


def _sma(vals, length):
    """SMA over a list possibly containing None (None propagates)."""
    out = [None] * len(vals)
    if length < 1:
        return out
    for i in range(length - 1, len(vals)):
        window = vals[i - length + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / length
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    k_len = max(2, int(inp.get("k_length", 14)))
    k_sm = max(1, int(inp.get("k_smooth", 3)))
    d_sm = max(1, int(inp.get("d_smooth", 3)))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    raw_k = [None] * n
    for i in range(k_len - 1, n):
        lo = min(l[i - k_len + 1:i + 1])
        hi = max(h[i - k_len + 1:i + 1])
        rng = hi - lo
        raw_k[i] = 50.0 if rng == 0 else 100.0 * (c[i] - lo) / rng
    pct_d = _sma(raw_k, k_sm)
    slow_d = _sma(pct_d, d_sm)
    times = bars["time"].astype(int).tolist()
    kc = inp.get("k_color", "#2962FF")
    dc = inp.get("d_color", "#FF6D00")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    for series, color in ((pct_d, kc), (slow_d, dc)):
        prev_t = prev_v = None
        for t, v in zip(times[start:], series[start:]):
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
    if inp.get("show_bands", True) and n:
        t0, t1 = int(times[0]), int(times[-1])
        lc = inp.get("band_color", "#787B86")
        for lvl in (float(inp.get("overbought", 80.0)),
                    float(inp.get("oversold", 20.0))):
            lines.append({"t0": t0, "t1": t1, "p0": lvl, "p1": lvl,
                          "color": lc, "width": 1, "style": "Dashed",
                          "label": "", "label_size": "Small"})
    last_k = next((v for v in reversed(raw_k) if v is not None), None)
    last_d = next((v for v in reversed(pct_d) if v is not None), None)
    last_slow = next((v for v in reversed(slow_d) if v is not None), None)
    values = {"stoch_k": last_k, "stoch_d": last_d, "stoch_slow_d": last_slow}
    dash = None
    if last_d is not None:
        dash = {"title": "Stochastic (%d, %d, %d)" % (k_len, k_sm, d_sm),
                "rows": [{"text": "%%K: %.1f | %%D: %.1f | Slow %%D: %.1f" % (
                    last_k if last_k is not None else 0.0, last_d,
                    last_slow if last_slow is not None else 0.0),
                    "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
