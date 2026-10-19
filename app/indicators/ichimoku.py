"""Ichimoku Kinko Hyo - Tenkan / Kijun / Senkou / Chikou + cloud.

Tenkan (conversion) = (highest high + lowest low over 9) / 2.
Kijun (base)       = (highest high + lowest low over 26) / 2.
Senkou A (leading span A) = (Tenkan + Kijun) / 2, plotted 26 bars ahead.
Senkou B (leading span B) = (highest high + lowest low over 52) / 2,
plotted 26 bars ahead.
Chikou (lagging span) = close, plotted 26 bars behind.
The cloud (Kumo) is the area between Senkou A and B - green while A
is on top, red while B is on top.

Displacement uses bar time + 26 * bar seconds (ctx["minutes"] * 60),
so the leading spans project past the last bar exactly like
The forward plot.

Overlay. Pure value - draws lines + cloud, never signals or alerts.
Closed-bar-only.
"""

META = {
    "id": "ichimoku",
    "name": "Ichimoku Cloud",
    "author": "StockSophist",
    "description": "Ichimoku Kinko Hyo (9 / 26 / 52 / 26) with cloud fill. "
                   "Reference only - never signals or alerts.",
    "overlay": True,
}

INPUTS = [
    {"key": "tenkan", "label": "Tenkan length", "type": "int",
     "default": 9, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "kijun", "label": "Kijun length", "type": "int",
     "default": 26, "min": 2, "max": 200, "group": "Inputs"},
    {"key": "senkou_b", "label": "Senkou B length", "type": "int",
     "default": 52, "min": 2, "max": 300, "group": "Inputs"},
    {"key": "displacement", "label": "Displacement", "type": "int",
     "default": 26, "min": 0, "max": 100, "group": "Inputs"},

    {"key": "tenkan_color", "label": "Tenkan color", "type": "color",
     "default": "#2962FF", "group": "Style"},
    {"key": "kijun_color", "label": "Kijun color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "senkou_a_color", "label": "Senkou A color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "senkou_b_color", "label": "Senkou B color", "type": "color",
     "default": "#EF5350", "group": "Style"},
    {"key": "chikou_color", "label": "Chikou color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 1, "min": 1, "max": 5, "group": "Style"},
    {"key": "show_tenkan", "label": "Show Tenkan", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_kijun", "label": "Show Kijun", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_senkou_a", "label": "Show Senkou A", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_senkou_b", "label": "Show Senkou B", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_chikou", "label": "Show Chikou", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_cloud", "label": "Show cloud fill", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "cloud_opacity", "label": "Cloud opacity", "type": "float",
     "default": 0.08, "min": 0.0, "max": 0.5, "group": "Style"},
]


def _mid(h, l, i, length):
    if i < length - 1:
        return None
    return (max(h[i - length + 1:i + 1]) +
            min(l[i - length + 1:i + 1])) / 2.0


def _segments(pairs, color, width):
    """pairs: [(t, v), ...] with None gaps -> line segments."""
    lines = []
    prev_t = prev_v = None
    for t, v in pairs:
        if v is None:
            prev_t = prev_v = None
            continue
        if prev_t is not None:
            lines.append({"t0": prev_t, "t1": int(t),
                          "p0": prev_v, "p1": float(v),
                          "color": color, "width": width, "style": "Solid",
                          "label": "", "label_size": "Small"})
        prev_t, prev_v = int(t), float(v)
    return lines


def compute(ctx):
    bars = ctx["bars"]
    inp = ctx["inputs"]
    n = len(bars)
    if n == 0:
        return {"lines": [], "dashboard": None, "sweeps": [], "values": {}}
    t_len = max(2, int(inp.get("tenkan", 9)))
    k_len = max(2, int(inp.get("kijun", 26)))
    b_len = max(2, int(inp.get("senkou_b", 52)))
    disp = max(0, int(inp.get("displacement", 26)))
    h = bars["high"].astype(float).tolist()
    l = bars["low"].astype(float).tolist()
    c = bars["close"].astype(float).tolist()
    times = bars["time"].astype(int).tolist()
    bar_sec = max(1, int(ctx.get("minutes") or 1)) * 60

    tenkan = [_mid(h, l, i, t_len) for i in range(n)]
    kijun = [_mid(h, l, i, k_len) for i in range(n)]
    senkou_a = [None if (t is None or k is None) else (t + k) / 2.0
                for t, k in zip(tenkan, kijun)]
    senkou_b = [_mid(h, l, i, b_len) for i in range(n)]

    width = int(inp.get("width", 1))
    lines = []
    start = max(1, n - 2000)
    # Tenkan / Kijun at their own bar times.
    if inp.get("show_tenkan", True):
        lines += _segments([(times[i], tenkan[i])
                            for i in range(start, n)],
                           inp.get("tenkan_color", "#2962FF"), width)
    if inp.get("show_kijun", True):
        lines += _segments([(times[i], kijun[i])
                            for i in range(start, n)],
                           inp.get("kijun_color", "#FF6D00"), width)
    # Leading spans displaced forward by `disp` bars.
    proj_t = [times[i] + disp * bar_sec for i in range(n)]
    if inp.get("show_senkou_a", True):
        lines += _segments([(proj_t[i], senkou_a[i])
                            for i in range(start, n)],
                           inp.get("senkou_a_color", "#26A69A"), width)
    if inp.get("show_senkou_b", True):
        lines += _segments([(proj_t[i], senkou_b[i])
                            for i in range(start, n)],
                           inp.get("senkou_b_color", "#EF5350"), width)
    # Chikou: close displaced backward by `disp` bars.
    if inp.get("show_chikou", True):
        lines += _segments([(times[i - disp], c[i])
                            for i in range(max(start, disp), n)],
                           inp.get("chikou_color", "#787B86"), width)

    # Cloud: area between Senkou A and B, split at every dominance flip
    # so each run carries its own color (A on top = green, B on top = red).
    areas = []
    if inp.get("show_cloud", True):
        opacity = float(inp.get("cloud_opacity", 0.08))
        run_t, run_up, run_lo, run_col = [], [], [], None
        pts = [(proj_t[i], senkou_a[i], senkou_b[i])
               for i in range(start, n)
               if senkou_a[i] is not None and senkou_b[i] is not None]
        for t, a, b in pts:
            col = "#26A69A" if a >= b else "#EF5350"
            if run_col is not None and col != run_col and len(run_t) > 1:
                areas.append({"t": run_t, "upper": run_up, "lower": run_lo,
                              "color": run_col, "opacity": opacity})
                run_t, run_up, run_lo = [], [], []
            run_col = col
            run_t.append(int(t))
            run_up.append(float(max(a, b)))
            run_lo.append(float(min(a, b)))
        if len(run_t) > 1:
            areas.append({"t": run_t, "upper": run_up, "lower": run_lo,
                          "color": run_col, "opacity": opacity})

    def _last(series):
        v = next((x for x in reversed(series) if x is not None), None)
        return None if v is None else float(v)

    values = {"tenkan": _last(tenkan), "kijun": _last(kijun),
              "senkou_a": _last(senkou_a), "senkou_b": _last(senkou_b),
              "chikou": float(c[-1]) if c else None}
    dash = None
    if values["tenkan"] is not None:
        dash = {"title": "Ichimoku (%d, %d, %d, %d)" % (t_len, k_len, b_len, disp),
                "rows": [{"text": "Tenkan: %.2f | Kijun: %.2f" % (
                    values["tenkan"], values["kijun"]), "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "areas": areas, "dashboard": dash,
            "sweeps": [], "values": values}
