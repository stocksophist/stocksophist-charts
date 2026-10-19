"""EMA Line - example built-in showing the indicator script format.

Copy this file into custom/ (or use "New script" in the app) and edit freely:
META describes the indicator, INPUTS defines its settings panel, and
compute(ctx) returns what to draw.
"""

META = {
    "id": "ema_line",
    "name": "EMA Line",
    "author": "StockSophist",
    "description": "Exponential moving average drawn as a line on the chart.",
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int", "default": 50,
     "min": 1, "max": 500, "group": "Settings"},
    {"key": "color", "label": "Line color", "type": "color", "default": "#2962ff",
     "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int", "default": 2,
     "min": 1, "max": 5, "group": "Style"},
]


def compute(ctx):
    bars = ctx["bars"]      # DataFrame: time, open, high, low, close (ascending)
    inp = ctx["inputs"]     # merged input values
    n = max(1, int(inp.get("length", 50)))

    ema = bars["close"].ewm(span=n, adjust=False).mean()
    lines, times = [], bars["time"].tolist()
    # draw the last 2000 bars as segments (cheap and smooth)
    start = max(1, len(times) - 2000)
    prev_t, prev_v = None, None
    for t, v in zip(times[start:], ema.tolist()[start:]):
        t = int(t)
        v = float(v)
        if prev_t is not None:
            lines.append({
                "t0": prev_t, "t1": t, "p0": prev_v, "p1": v,
                "color": inp.get("color", "#2962ff"),
                "width": int(inp.get("width", 2)),
                "style": "Solid", "label": "", "label_size": "Small",
            })
        prev_t, prev_v = t, v
    # latest numeric value for the AI snapshot (flat dict).
    ema_vals = ema.tolist()
    last = float(ema_vals[-1]) if ema_vals else None
    values = {"ema%d" % n: last} if last is not None else {}
    return {"lines": lines, "dashboard": None, "sweeps": [],
            "values": values}
