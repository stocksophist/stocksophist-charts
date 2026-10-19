"""Volume - bar-volume histogram.

One vertical bar per candle: height = the bar's volume, colored by the
bar's direction (close >= open = up, else down). Volume is MT5 tick
volume (the number of price updates per bar - spot gold has no central
exchange, so this is the standard activity proxy).

Separate pane - add it to a dedicated pane below the chart. Pure
value - draws bars, never signals or alerts. Closed-bar-only.

No volume data (bars without a volume column, or all-zero volume) ->
draws nothing and says so honestly in the dashboard.
"""

META = {
    "id": "volume",
    "name": "Volume",
    "author": "StockSophist",
    "description": "Bar-volume histogram, colored by bar direction. "
                   "Reference only - never signals or alerts.",
    "overlay": False,
}

INPUTS = [
    {"key": "up_color", "label": "Up bar color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "down_color", "label": "Down bar color", "type": "color",
     "default": "#EF5350", "group": "Style"},
    {"key": "width", "label": "Bar width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
]


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    if "volume" not in bars.columns:
        dash = {"title": "Volume",
                "rows": [{"text": "No volume data for this symbol",
                          "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
        return {"lines": [], "dashboard": dash, "sweeps": [], "values": {}}
    times = bars["time"].astype(int).tolist()
    o = bars["open"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    vol = bars["volume"].fillna(0).astype(float).tolist()
    if not any(v > 0 for v in vol):
        dash = {"title": "Volume",
                "rows": [{"text": "No volume data for this symbol",
                          "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
        return {"lines": [], "dashboard": dash, "sweeps": [], "values": {}}
    up_c = inp.get("up_color", "#26A69A")
    dn_c = inp.get("down_color", "#EF5350")
    width = int(inp.get("width", 1))
    lines = []
    start = max(0, n - 2000)
    for t, oo, cc, v in zip(times[start:], o[start:], c[start:], vol[start:]):
        if v is None or v <= 0:
            continue
        lines.append({"t0": int(t), "t1": int(t), "p0": 0.0, "p1": float(v),
                      "color": up_c if cc >= oo else dn_c,
                      "width": width, "style": "Solid",
                      "label": "", "label_size": "Small"})
    last = float(vol[-1]) if vol else 0.0
    avg = sum(vol) / len(vol) if vol else 0.0
    dash = {"title": "Volume",
            "rows": [{"text": "Volume: %.0f (avg %.0f)" % (last, avg),
                      "color": "#ffffff"}],
            "position": "Bottom Right", "size": "Normal",
            "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [],
            "values": {"volume": last}}
