"""MACD - Moving Average Convergence Divergence (12, 26, 9).

MACD line = EMA(12) - EMA(26) of close. Signal line = EMA(9) of MACD.
Histogram = MACD - Signal. EMAs use the standard k = 2/(n+1) smoothing,
seeded with the SMA of the first n values (a standard warm-up choice;
note: this differs from pandas ewm(adjust=False), which seeds with the
first value - the two converge after the warm-up window).

Pure value - draws MACD + Signal lines, dashboard with latest values.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "macd",
    "name": "MACD",
    "author": "StockSophist",
    "description": "MACD (12, 26, 9): MACD line, Signal line, Histogram. "
                   "Reference only, never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "fast", "label": "Fast length", "type": "int",
     "default": 12, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "slow", "label": "Slow length", "type": "int",
     "default": 26, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "signal", "label": "Signal length", "type": "int",
     "default": 9, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "macd_color", "label": "MACD color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "signal_color", "label": "Signal color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "hist_up_color", "label": "Histogram up color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "hist_down_color", "label": "Histogram down color", "type": "color",
     "default": "#EF5350", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "show_macd", "label": "Show MACD line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_signal", "label": "Show signal line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_hist", "label": "Show histogram", "type": "bool",
     "default": True, "group": "Style"},
]


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
    fast = max(2, int(inp.get("fast", 12)))
    slow = max(2, int(inp.get("slow", 26)))
    sig_len = max(2, int(inp.get("signal", 9)))
    c = bars["close"].astype(float).tolist()
    ema_fast = compute_ema_series(c, fast)
    ema_slow = compute_ema_series(c, slow)
    macd = [None if (a is None or b is None) else a - b
            for a, b in zip(ema_fast, ema_slow)]
    # Signal = EMA of MACD over its defined values (re-indexed).
    sig = [None] * n
    defined = [(i, v) for i, v in enumerate(macd) if v is not None]
    if len(defined) >= sig_len:
        vals = [v for _, v in defined]
        ema_sig = compute_ema_series(vals, sig_len)
        for (i, _v), sv in zip(defined, ema_sig):
            sig[i] = sv
    hist = [None if (a is None or b is None) else a - b
            for a, b in zip(macd, sig)]
    times = bars["time"].astype(int).tolist()
    mc = inp.get("macd_color", "#2962FF")
    sc = inp.get("signal_color", "#FF6D00")
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    for series, color, show in ((macd, mc, inp.get("show_macd", True)),
                                (sig, sc, inp.get("show_signal", True))):
        if not show:
            continue
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
    # Histogram: one vertical bar per candle from the zero line,
    # colored by sign (up = green, down = red).
    if inp.get("show_hist", True):
        hu = inp.get("hist_up_color", "#26A69A")
        hd = inp.get("hist_down_color", "#EF5350")
        for t, v in zip(times[start:], hist[start:]):
            if v is None:
                continue
            lines.append({"t0": int(t), "t1": int(t), "p0": 0.0,
                          "p1": float(v),
                          "color": hu if v >= 0 else hd,
                          "width": 1, "style": "Solid",
                          "label": "", "label_size": "Small"})
    last_macd = next((v for v in reversed(macd) if v is not None), None)
    last_sig = next((v for v in reversed(sig) if v is not None), None)
    last_hist = next((v for v in reversed(hist) if v is not None), None)
    values = {"macd": last_macd, "macd_signal": last_sig,
              "macd_hist": last_hist}
    dash = None
    if last_macd is not None:
        dash = {"title": "MACD (%d, %d, %d)" % (fast, slow, sig_len),
                "rows": [{"text": "MACD: %.2f | Signal: %.2f | Hist: %.2f" % (
                    last_macd, last_sig if last_sig is not None else 0.0,
                    last_hist if last_hist is not None else 0.0),
                    "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
