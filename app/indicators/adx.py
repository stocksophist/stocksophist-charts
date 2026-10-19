"""ADX - Average Directional Index (Wilder, 14).

+DM = max(H - prevH, 0) if (H - prevH) > (prevL - L) else 0.
-DM = max(prevL - L, 0) if (prevL - L) > (H - prevH) else 0.
TR = max(H - L, |H - prevC|, |L - prevC|).
+DI = 100 * Wilder(+DM) / Wilder(TR); -DI likewise.
DX = 100 * |+DI - -DI| / (+DI + -DI) (0 when the sum is 0).
ADX = Wilder-smoothed DX (first ADX = SMA of first `length` DX values).

Pure value - draws the ADX line, dashboard with ADX/+DI/-DI.
Never signals or alerts. Closed-bar-only.
"""

META = {
    "id": "adx",
    "name": "ADX",
    "author": "StockSophist",
    "description": "Average Directional Index (Wilder) with +DI/-DI and a "
                   "25 trend-strength level. Reference only, never signals.",
    "overlay": False,
}

INPUTS = [
    {"key": "length", "label": "Length", "type": "int",
     "default": 14, "min": 2, "max": 100, "group": "Inputs"},
    {"key": "level", "label": "Trend level", "type": "float",
     "default": 25.0, "min": 0, "max": 100, "group": "Inputs"},
    {"key": "adx_color", "label": "ADX color", "type": "color",
     "default": "#FF6D00", "group": "Style"},
    {"key": "plus_color", "label": "+DI color", "type": "color",
     "default": "#26A69A", "group": "Style"},
    {"key": "minus_color", "label": "-DI color", "type": "color",
     "default": "#EF5350", "group": "Style"},
    {"key": "level_color", "label": "Level color", "type": "color",
     "default": "#787B86", "group": "Style"},
    {"key": "width", "label": "Line width", "type": "int",
     "default": 2, "min": 1, "max": 5, "group": "Style"},
    {"key": "show_adx", "label": "Show ADX line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_plus", "label": "Show +DI line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_minus", "label": "Show -DI line", "type": "bool",
     "default": True, "group": "Style"},
    {"key": "show_level", "label": "Show trend level", "type": "bool",
     "default": True, "group": "Style"},
]


def _wilder_smooth(vals, length, start_idx):
    """Wilder smoothing over vals[start_idx:], first value = SMA of first
    `length` values. Returns list aligned with vals (None before start)."""
    out = [None] * len(vals)
    if len(vals) - start_idx < length:
        return out
    seed = vals[start_idx:start_idx + length]
    sm = sum(seed) / length
    out[start_idx + length - 1] = sm
    for i in range(start_idx + length, len(vals)):
        sm = (sm * (length - 1) + vals[i]) / length
        out[i] = sm
    return out


def compute_adx_series(h, l, c, length):
    """Returns (adx, plus_di, minus_di) lists, None until warmed up."""
    n = len(h)
    adx = [None] * n
    pdi = [None] * n
    mdi = [None] * n
    if n <= 2 * length:
        return adx, pdi, mdi
    pdm = [0.0] * n
    mdm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    sm_pdm = _wilder_smooth(pdm, length, 1)
    sm_mdm = _wilder_smooth(mdm, length, 1)
    sm_tr = _wilder_smooth(tr, length, 1)
    dx = [None] * n
    for i in range(n):
        if sm_tr[i] and sm_tr[i] != 0:
            pdi[i] = 100.0 * sm_pdm[i] / sm_tr[i]
            mdi[i] = 100.0 * sm_mdm[i] / sm_tr[i]
            s = pdi[i] + mdi[i]
            dx[i] = 100.0 * abs(pdi[i] - mdi[i]) / s if s != 0 else 0.0
    # ADX = Wilder-smoothed DX; first ADX = SMA of first `length` DX.
    first_dx = next((i for i, v in enumerate(dx) if v is not None), None)
    if first_dx is None:
        return adx, pdi, mdi
    if n - first_dx < length:
        return adx, pdi, mdi
    seed = [dx[i] for i in range(first_dx, first_dx + length)]
    sm = sum(seed) / length
    adx[first_dx + length - 1] = sm
    for i in range(first_dx + length, n):
        if dx[i] is None:
            continue
        sm = (sm * (length - 1) + dx[i]) / length
        adx[i] = sm
    return adx, pdi, mdi


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
    adx, pdi, mdi = compute_adx_series(h, l, c, length)
    times = bars["time"].astype(int).tolist()
    width = int(inp.get("width", 2))
    lines = []
    start = max(1, n - 2000)
    for series, color, show in (
            (adx, inp.get("adx_color", "#FF6D00"), inp.get("show_adx", True)),
            (pdi, inp.get("plus_color", "#26A69A"), inp.get("show_plus", True)),
            (mdi, inp.get("minus_color", "#EF5350"),
             inp.get("show_minus", True))):
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
    # Trend-strength level (full-width horizontal dashed segment).
    if inp.get("show_level", True) and n:
        lvl = float(inp.get("level", 25.0))
        lines.append({"t0": int(times[0]), "t1": int(times[-1]),
                      "p0": lvl, "p1": lvl,
                      "color": inp.get("level_color", "#787B86"),
                      "width": 1, "style": "Dashed",
                      "label": "", "label_size": "Small"})
    last_adx = next((v for v in reversed(adx) if v is not None), None)
    last_pdi = next((v for v in reversed(pdi) if v is not None), None)
    last_mdi = next((v for v in reversed(mdi) if v is not None), None)
    values = {"adx%d" % length: last_adx, "plus_di": last_pdi,
              "minus_di": last_mdi}
    dash = None
    if last_adx is not None:
        dash = {"title": "ADX (%d)" % length,
                "rows": [{"text": "ADX: %.1f | +DI: %.1f | -DI: %.1f" % (
                    last_adx, last_pdi or 0.0, last_mdi or 0.0),
                    "color": "#ffffff"}],
                "position": "Bottom Right", "size": "Normal",
                "bg": "#111111", "fg": "#ffffff"}
    return {"lines": lines, "dashboard": dash, "sweeps": [], "values": values}
