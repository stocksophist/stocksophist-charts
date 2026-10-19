"""Indicator library: built-in + user-scripted indicators, script-style.

Each indicator is a Python file exposing:
    META   = {"id": ..., "name": ..., "author": ..., "description": ...,
              "overlay": True|False}   # True = draws on the price chart,
                                        # False = separate pane (oscillator)
    INPUTS = [ {"key","label","type","default", ...}, ... ]   # settings schema
    compute(ctx) -> {"lines": [...], "dashboard": {...}|None, "sweeps": [...]}

Optional compute outputs: "areas" (filled polygons between two series:
[{"t": [...], "upper": [...], "lower": [...], "color": str,
"opacity": float}]) and "boxes" (rectangles: {"t0","t1","p0","p1",
"bgcolor", ...} - used for dot-style plots like Parabolic SAR).

Input types: bool, int, float, string, color, options.
Line: {"t0":ts, "t1":ts|None, "p0":float, "p1":float, "color":str,
       "width":int, "style":"Solid|Dashed|Dotted",
       "label":str, "label_size":"Tiny|Small|Normal|Large|Huge"}
       (p0/p1 optional - defaults to horizontal at "price")
Sweep: {"key":str, "text":str, "price":float, "alert":bool}
Dashboard: {"title":str, "rows":[{"text","color"}], "position":str,
            "size":str, "bg":str, "fg":str}

Built-ins live in this directory; user scripts live in custom/.
Favorites are stored in favorites.json. Like the chart app: Favorites /
My scripts / Built-in, searchable, star to favorite.
"""

import importlib.util
import json
import os
import re

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_DIR = os.path.join(LIB_DIR, "custom")
FAV_FILE = os.path.join(LIB_DIR, "favorites.json")

os.makedirs(CUSTOM_DIR, exist_ok=True)


def set_runtime_dir(path):
    """keep user data (favorites.json, "My scripts" custom
    indicators) out of the code tree so it survives delete-and-replace
    updates. server.py calls this once with DATA_DIR/indicators.
    Existing in-tree user files are copied over."""
    global CUSTOM_DIR, FAV_FILE
    import shutil as _shutil
    d = os.path.abspath(path)
    os.makedirs(d, exist_ok=True)
    new_custom = os.path.join(d, "custom")
    os.makedirs(new_custom, exist_ok=True)
    # Migrate user-authored scripts (never overwrite the new copies).
    try:
        for fn in os.listdir(CUSTOM_DIR):
            if fn.endswith(".py") and not fn.startswith("_"):
                src, dst = os.path.join(CUSTOM_DIR, fn), os.path.join(new_custom, fn)
                if not os.path.exists(dst):
                    _shutil.copy2(src, dst)
    except Exception:
        pass
    # Migrate starred-indicator favorites.
    try:
        if not os.path.exists(os.path.join(d, "favorites.json")) and \
                os.path.exists(FAV_FILE):
            _shutil.copy2(FAV_FILE, os.path.join(d, "favorites.json"))
    except Exception:
        pass
    CUSTOM_DIR = new_custom
    FAV_FILE = os.path.join(d, "favorites.json")

_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
REQUIRED_ATTRS = ("META", "INPUTS", "compute")


def _load_module(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _validate(mod):
    for attr in REQUIRED_ATTRS:
        if not hasattr(mod, attr):
            return f"missing required '{attr}'"
    meta = mod.META
    if not isinstance(meta, dict) or not meta.get("id") or not meta.get("name"):
        return "META must be a dict with 'id' and 'name'"
    if not _ID_RE.match(str(meta["id"])):
        return "META id must be 2-40 chars: lowercase letters, digits, _"
    if not isinstance(mod.INPUTS, list):
        return "INPUTS must be a list"
    if not callable(mod.compute):
        return "compute must be a function"
    return None


def _favorites():
    try:
        with open(FAV_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_favorites(favs):
    try:
        with open(FAV_FILE, "w") as f:
            json.dump(sorted(favs), f)
    except Exception:
        pass


def _iter_files():
    out = []
    for fn in sorted(os.listdir(LIB_DIR)):
        if fn.startswith("_") or not fn.endswith(".py") or fn == "__init__.py":
            continue
        out.append((os.path.join(LIB_DIR, fn), True))
    for fn in sorted(os.listdir(CUSTOM_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        out.append((os.path.join(CUSTOM_DIR, fn), False))
    return out


def list_indicators():
    """Catalogue for the library dialog. Returns (indicators, errors)."""
    favs = _favorites()
    seen, out, errors = set(), [], []
    for path, builtin in _iter_files():
        fn = os.path.basename(path)
        try:
            mod = _load_module(path, "indlib_" + fn[:-3])
            err = _validate(mod)
            if err:
                errors.append({"file": fn, "error": err})
                continue
            mid = str(mod.META["id"])
            if mid in seen:
                continue
            seen.add(mid)
            out.append({
                "id": mid,
                "name": str(mod.META.get("name", mid)),
                "author": str(mod.META.get("author", "")),
                "description": str(mod.META.get("description", "")),
                "attribution": str(mod.META.get("attribution", "")),
                "overlay": bool(mod.META.get("overlay", True)),
                "builtin": builtin,
                "favorite": mid in favs,
                "inputs": mod.INPUTS,
            })
        except Exception as e:
            errors.append({"file": fn,
                           "error": "%s: %s" % (type(e).__name__, e)})
            continue
    return out, errors


def get_indicator(ind_id):
    for path, _ in _iter_files():
        try:
            mod = _load_module(path, "indlib_" + os.path.basename(path)[:-3])
            if _validate(mod):
                continue
            if str(mod.META["id"]) == ind_id:
                return mod
        except Exception:
            continue
    return None


def default_inputs(mod):
    d = {}
    for i in (mod.INPUTS or []):
        if isinstance(i, dict) and "key" in i:
            d[i["key"]] = i.get("default")
    return d


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


def validate_indicator_settings(mod, settings):
    """Validate + coerce a settings dict against the indicator's INPUTS
    schema. Returns (clean_dict, error_or_None). Unknown keys are
    rejected; int/float are clamped to their min/max; color must be
    #RRGGBB or #RRGGBBAA; options must be in the declared list.
    Used by the /api/indicator_settings endpoint AND by tests, so the
    endpoint itself stays a thin wrapper."""
    if not isinstance(settings, dict):
        return None, "settings must be an object"
    schema = {}
    for i in (mod.INPUTS or []):
        if isinstance(i, dict) and "key" in i:
            schema[i["key"]] = i
    clean = {}
    for k, v in settings.items():
        spec = schema.get(k)
        if spec is None:
            return None, "unknown setting '%s' for %s" % (
                k, mod.META.get("id"))
        t = spec.get("type")
        try:
            if t == "bool":
                if not isinstance(v, bool):
                    return None, "setting '%s' must be true/false" % k
                clean[k] = v
            elif t == "int":
                if isinstance(v, bool):
                    return None, "setting '%s' must be an integer" % k
                iv = int(v)
                if "min" in spec:
                    iv = max(int(spec["min"]), iv)
                if "max" in spec:
                    iv = min(int(spec["max"]), iv)
                clean[k] = iv
            elif t == "float":
                if isinstance(v, bool):
                    return None, "setting '%s' must be a number" % k
                fv = float(v)
                if "min" in spec:
                    fv = max(float(spec["min"]), fv)
                if "max" in spec:
                    fv = min(float(spec["max"]), fv)
                clean[k] = fv
            elif t == "color":
                s = str(v).strip()
                if not _COLOR_RE.match(s):
                    return None, ("setting '%s' must be a #RRGGBB color"
                                  % k)
                clean[k] = s
            elif t == "options":
                opts = spec.get("options") or []
                if v not in opts:
                    return None, ("setting '%s' must be one of %s"
                                  % (k, opts))
                clean[k] = v
            else:  # string and anything else: store as text
                clean[k] = str(v)
        except (TypeError, ValueError):
            return None, "setting '%s' has an invalid value" % k
    return clean, None


def merge_indicator_settings(mod, overrides):
    """Factory defaults <- saved global overrides (unknown keys ignored,
    so a stale override from an older version can never inject junk)."""
    out = default_inputs(mod)
    for k, v in (overrides or {}).items():
        if k in out:
            out[k] = v
    return out


def apply_indicator_settings(saved, ind_id, body):
    """Pure POST semantics for /api/indicator_settings (no I/O).
    saved: the stored indicator_settings dict (never mutated).
    body: the posted JSON body.
    Returns (new_saved_dict, payload, http_status). The server endpoint
    is a thin wrapper: load settings.json -> apply -> save -> jsonify."""
    if not isinstance(body, dict):
        return saved, {"error": "body must be an object"}, 400
    if body.get("reset"):
        new_saved = dict(saved or {})
        new_saved.pop(ind_id, None)
        return (new_saved,
                {"ok": True, "indicator": ind_id, "reset": True}, 200)
    settings = body.get("settings")
    if not isinstance(settings, dict):
        return saved, {"error": "settings must be an object"}, 400
    mod = get_indicator(ind_id)
    if mod is None:
        return saved, {"error": "unknown indicator: %s" % ind_id}, 400
    clean, err = validate_indicator_settings(mod, settings)
    if err:
        return saved, {"error": err}, 400
    new_saved = dict(saved or {})
    new_saved[ind_id] = clean
    return (new_saved,
            {"ok": True, "indicator": ind_id, "settings": clean}, 200)


def add_custom(ind_id, name, code):
    """Validate and save a user script. Returns (ok, message)."""
    ind_id = (ind_id or "").strip().lower()
    if not _ID_RE.match(ind_id):
        return False, "id must be 2-40 chars: lowercase letters, digits, _"
    if get_indicator(ind_id):
        return False, f"an indicator with id '{ind_id}' already exists"
    try:
        compile(code, "<indicator>", "exec")
    except SyntaxError as e:
        return False, f"syntax error line {e.lineno}: {e.msg}"
    tmp_path = os.path.join(CUSTOM_DIR, f"_{ind_id}_tmp.py")
    try:
        with open(tmp_path, "w") as f:
            f.write(code)
        mod = _load_module(tmp_path, "indlib_tmp_" + ind_id)
        err = _validate(mod)
        if err:
            return False, "invalid indicator: " + err
        if str(mod.META["id"]) != ind_id:
            return False, f"META id '{mod.META['id']}' must match '{ind_id}'"
        # smoke-test compute with tiny bars
        import pandas as pd
        bars = pd.DataFrame({
            "time": list(range(1, 61)),
            "open": [100.0] * 60, "high": [101.0] * 60,
            "low": [99.0] * 60, "close": [100.0] * 60,
        })
        ctx = {"symbol": "TEST", "tf": "1m", "bars": bars,
               "inputs": default_inputs(mod), "minutes": 1,
               "get_bars": lambda tf: bars}
        res = mod.compute(ctx)
        if not isinstance(res, dict) or "lines" not in res:
            return False, "compute(ctx) must return a dict with a 'lines' list"
    except Exception as e:
        return False, f"validation failed: {e}"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    final = os.path.join(CUSTOM_DIR, ind_id + ".py")
    header = f'"""{name} - user script, added from the chart app."""\n'
    with open(final, "w") as f:
        f.write(header + code)
    return True, f"'{name}' added to My scripts"


def delete_custom(ind_id):
    mod = get_indicator(ind_id)
    if mod is None:
        return False, "indicator not found"
    path = os.path.join(CUSTOM_DIR, ind_id + ".py")
    if not os.path.exists(path):
        return False, "built-in indicators cannot be deleted"
    try:
        os.remove(path)
    except OSError as e:
        return False, str(e)
    favs = _favorites()
    favs.discard(ind_id)
    _save_favorites(favs)
    return True, "deleted"


def set_favorite(ind_id, fav):
    if get_indicator(ind_id) is None:
        return False
    favs = _favorites()
    if fav:
        favs.add(ind_id)
    else:
        favs.discard(ind_id)
    _save_favorites(favs)
    return True
