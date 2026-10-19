"""OBV - On-Balance Volume (Granville).

Cumulative total: OBV[i] = OBV[i-1] + volume[i] if close[i] > close[i-1],
OBV[i-1] - volume[i] if close[i] < close[i-1], unchanged on a flat bar.
Starts at 0. OBV itself has no natural scale - read it for trend and
divergence against price, not for absolute levels.

Volume is MT5 tick volume (the number of price updates per bar). Bars
without volume data contribute 0, so OBV still tracks direction.

Separate pane. Pure value - draws a line, never signals or alerts.
Closed-bar-only.
"""

META = {
    "id": "obv",
    "name": "OBV",
    "author": "StockSophist",
    "description": "On-Balance Volume (Granville). Cumulative volume signed by "
                   "price direction. Reference only - never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "color", "label": "Line color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "style", "label": "Line style", "type": "options",
     "options": ["Solid", "Dashed", "Dotted"], "default": "Solid",
     "group": "Style"},
]


def compute_obv_series(closes, vols):
    """Cumulative OBV. vols may be all zeros (no volume data)."""
    out = []
    obv = 0.0
    for i, c in enumerate(closes):
        if i > 0:
            v = vols[i] if i < len(vols) else 0.0
            if c > closes[i - 1]:
                obv += v
            elif c < closes[i - 1]:
                obv -= v
        out.append(obv)
    return out


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    c = bars["close"].astype(float).tolist()
    if "volume" in bars.columns:
        vol = bars["volume"].fillna(0).astype(float).tolist()
    else:
        vol = [0.0] * n
    obv = compute_obv_series(c, vol)
    times = bars["time"].astype(int).tolist()
    color = inp.get("color", "#2962FF")
    width = int(inp.get("width", 2))
    style = inp.get("style", "Solid")
    lines = []
    start = max(1, n - 2000)
    prev_t = prev_v = None
    for t, v in zip(times[start:], obv[start:]):
        t = int(t)
        v = float(v)
        if prev_t is not None:
            lines.append({"t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                          "color": color, "width": width, "style": style,
                          "label": "", "label_size": "Small"})
        prev_t, prev_v = t, v
    last = float(obv[-1]) if obv else 0.0
    dash = {"title": "OBV",
            "rows": [{"text": "OBV: %.0f" % last, "color": "#ffffff"}],
            "position": "Bottom Right", "size": "Normal",
            "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [],
            "values": {"obv": last}}
