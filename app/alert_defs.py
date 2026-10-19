"""StockSophist Charts - alert definitions + trigger log.

Server-side home for chart-style alerts:
  * Alert definitions (price alerts + per-indicator alerts) in app/alerts.json
    with chart-like lifecycle states:
    active | stopped_manual | stopped_triggered | stopped_expired
  * Trigger log in app/alert_log.json (capped, newest-first, CSV export).
  * /fire evaluation: cooldown / frequency / expiry / anti-spam
    (>15 triggers in 3 min -> auto-stop, mirroring the chart app's guard).
  * Persistent sweep dedup keys so a restart can never refire a sweep.

All writes are atomic (tmp file + os.replace), same pattern as settings.
"""

import json
import os
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ALERTS_FILE = os.path.join(HERE, "alerts.json")
LOG_FILE = os.path.join(HERE, "alert_log.json")
LOG_CAP = 5000


def set_data_dir(path):
    """redirect runtime files (alerts.json, alert_log.json) out of
    the app tree so user price alerts survive delete-and-replace updates.
    server.py calls this once with its DATA_DIR; one-time migration copies
    any existing in-tree files over."""
    global ALERTS_FILE, LOG_FILE
    import shutil as _shutil
    d = os.path.abspath(path)
    os.makedirs(d, exist_ok=True)
    for old, name in ((ALERTS_FILE, "alerts.json"), (LOG_FILE, "alert_log.json")):
        new = os.path.join(d, name)
        if not os.path.exists(new) and os.path.exists(old):
            try:
                _shutil.copy2(old, new)
            except Exception:
                pass
    ALERTS_FILE = os.path.join(d, "alerts.json")
    LOG_FILE = os.path.join(d, "alert_log.json")

# Anti-spam: mirrors the documented "triggered too often" guard.
ANTISPAM_WINDOW = 180      # seconds
ANTISPAM_MAX = 15          # triggers inside the window -> auto-stop

# Alert timeframes every indicator alert stores the timeframe its
# indicator is evaluated on - like the chart app's alert dialog. The background
# Alert Engine evaluates the alert on THAT timeframe only, no matter which
# timeframe any chart pane shows (or whether the browser is open at all).
_ALERT_TFS = ("1s", "15s", "1m", "3m", "5m", "15m", "30m", "1H", "4H", "D", "W", "M")


def _norm_tf(v):
    v = str(v or "").strip()
    return v if v in _ALERT_TFS else ""

_lock = threading.Lock()

STATUS_ACTIVE = "active"
STATUS_MANUAL = "stopped_manual"
STATUS_TRIGGERED = "stopped_triggered"
STATUS_EXPIRED = "stopped_expired"
STATUSES = (STATUS_ACTIVE, STATUS_MANUAL, STATUS_TRIGGERED, STATUS_EXPIRED)

STATUS_LABEL = {
    STATUS_ACTIVE: "Active",
    STATUS_MANUAL: "Stopped manually",
    STATUS_TRIGGERED: "Stopped - Triggered",
    STATUS_EXPIRED: "Stopped - Expired",
}


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return int(time.time())


def _uid(prefix):
    return prefix + uuid.uuid4().hex[:12]


# --------------------------------------------------------------- definitions
def _blank_store():
    return {"alerts": [], "fired_keys": {}}


def _load_store():
    d = _read_json(ALERTS_FILE, None)
    if not isinstance(d, dict) or not isinstance(d.get("alerts"), list):
        return _blank_store()
    if not isinstance(d.get("fired_keys"), dict):
        d["fired_keys"] = {}
    return d


def _save_store(store):
    _write_json(ALERTS_FILE, store)


def _norm_alert(a):
    n = {
        "id": str(a.get("id") or _uid("a_")),
        "kind": a.get("kind") if a.get("kind") in ("price", "indicator") else "price",
        "symbol": str(a.get("symbol") or "").strip(),
        "timeframe": _norm_tf(a.get("timeframe")),
        # Chart mode the alert was created from "renko" means the
        # engine evaluates the indicator on Renko bricks (with `box`), not
        # candles. Empty = candles (legacy default).
        "chart_mode": "renko" if str(a.get("chart_mode") or "") == "renko" else "",
        "box": max(0.1, min(float(a.get("box") or 2.5), 50.0)),
        # Indicator inputs snapshot the background engine evaluates
        # the indicator on `timeframe` with exactly these settings, so the
        # alert keeps working even if the pane's settings change later -
        # same as snapshotting the script's inputs.
        "settings": dict(a.get("settings") or {}) if isinstance(a.get("settings"), dict) else {},
        # Newest closed bar (epoch) the engine has evaluated.
        "last_eval_bar": int(a.get("last_eval_bar") or 0),
        "name": str(a.get("name") or "").strip(),
        "indicator_id": str(a.get("indicator_id") or ""),
        "indicator_name": str(a.get("indicator_name") or ""),
        "condition": str(a.get("condition") or "crossing"),
        "price": a.get("price"),
        "note": str(a.get("note") or ""),
        "freq": "every" if a.get("freq") == "every" else "once",
        "cooldown_min": max(0, float(a.get("cooldown_min") or 0)),
        "expires_at": a.get("expires_at"),
        "notify": {
            "popup": True, "sound": True, "telegram": True, "webhook": True,
            **(a.get("notify") or {}),
        },
        "message": str(a.get("message") or "{{text}}"),
        "webhook_url": str(a.get("webhook_url") or ""),
        "status": a.get("status") if a.get("status") in STATUSES else STATUS_ACTIVE,
        "created_at": int(a.get("created_at") or _now()),
        "last_triggered_at": a.get("last_triggered_at"),
        "trigger_count": int(a.get("trigger_count") or 0),
        "fire_times": [t for t in (a.get("fire_times") or []) if isinstance(t, (int, float))],
    }
    if not n["name"]:
        if n["kind"] == "price" and n["price"] is not None:
            n["name"] = "%s Crossing %s" % (n["symbol"], n["price"])
        elif n["indicator_name"]:
            n["name"] = "%s - %s" % (n["indicator_name"], n["symbol"])
        else:
            n["name"] = n["symbol"] or "Alert"
    return n


def list_alerts():
    with _lock:
        return [_norm_alert(a) for a in _load_store()["alerts"]]


def get_alert(alert_id):
    with _lock:
        for a in _load_store()["alerts"]:
            if str(a.get("id")) == str(alert_id):
                return _norm_alert(a)
    return None


def create_alert(data):
    with _lock:
        store = _load_store()
        a = _norm_alert(data or {})
        store["alerts"].insert(0, a)
        _save_store(store)
    log_system(a["id"], a["kind"], "created",
               "Alert created: %s" % a["name"])
    return a


def update_alert(alert_id, data):
    data = data or {}
    with _lock:
        store = _load_store()
        for i, a in enumerate(store["alerts"]):
            if str(a.get("id")) == str(alert_id):
                merged = dict(a)
                for k in ("symbol", "name", "indicator_id", "indicator_name",
                          "condition", "price", "note", "freq", "cooldown_min",
                          "expires_at", "notify", "message", "webhook_url", "status",
                          "timeframe", "settings", "last_eval_bar"):
                    if k in data:
                        merged[k] = data[k]
                na = _norm_alert(merged)
                # changing core fields on a triggered once-alert reactivates it
                store["alerts"][i] = na
                _save_store(store)
                return na
    return None


def delete_alert(alert_id):
    with _lock:
        store = _load_store()
        kept = [a for a in store["alerts"] if str(a.get("id")) != str(alert_id)]
        if len(kept) == len(store["alerts"]):
            return False
        store["alerts"] = kept
        _save_store(store)
    log_system(alert_id, "", "deleted", "Alert deleted")
    return True


def set_status(alert_id, status):
    if status not in STATUSES:
        return None
    a = update_alert(alert_id, {"status": status})
    if a:
        log_system(alert_id, a["kind"],
                   "stopped" if status != STATUS_ACTIVE else "resumed",
                   "%s: %s" % (STATUS_LABEL[status], a["name"]))
    return a


def find_indicator_alert(indicator_id, symbol):
    with _lock:
        for a in _load_store()["alerts"]:
            if (a.get("kind") == "indicator"
                    and str(a.get("indicator_id")) == str(indicator_id)
                    and str(a.get("symbol")) == str(symbol)):
                return _norm_alert(a)
    return None


def ensure_indicator_alert(indicator_id, indicator_name, symbol,
                           timeframe="", settings=None,
                           chart_mode="", box=0):
    """Find-or-create the per-(indicator, symbol) alert definition.

    also stamps `timeframe` + `settings` when the stored definition
    lacks them, so alerts created before timeframes existed (or auto-created
    on first fire) join the background engine without user action. A
    timeframe the user chose explicitly is never overwritten here.

    also stamps `chart_mode` ("renko" when the alert was created from
    a Renko pane) + `box` (the Renko box size). A Renko alert must be
    evaluated on Renko bricks, not candles - the engine reads these back.
    An explicitly chosen mode is never overwritten here either.
    """
    a = find_indicator_alert(indicator_id, symbol)
    patch = {}
    if timeframe and not (a or {}).get("timeframe"):
        patch["timeframe"] = timeframe
    if settings and not (a or {}).get("settings"):
        patch["settings"] = settings
    if chart_mode and not (a or {}).get("chart_mode"):
        patch["chart_mode"] = chart_mode
    if box and not (a or {}).get("box"):
        try:
            patch["box"] = float(box)
        except (TypeError, ValueError):
            pass
    if a:
        return update_alert(a["id"], patch) or a
    try:
        box_v = float(box) if box else 0
    except (TypeError, ValueError):
        box_v = 0
    return create_alert({
        "kind": "indicator",
        "symbol": symbol,
        "indicator_id": indicator_id,
        "indicator_name": indicator_name or indicator_id,
        "condition": "any_signal",
        "freq": "every",
        "timeframe": timeframe,
        "settings": settings or {},
        "chart_mode": chart_mode or "",
        "box": box_v,
    })


def sweep_already_fired(sweep_key):
    with _lock:
        return sweep_key in _load_store()["fired_keys"]


def mark_sweep_fired(sweep_key, alert_id):
    with _lock:
        store = _load_store()
        store["fired_keys"][sweep_key] = {"alert_id": alert_id, "ts": _now()}
        # prune keys older than 7 days
        cutoff = _now() - 7 * 86400
        store["fired_keys"] = {k: v for k, v in store["fired_keys"].items()
                               if isinstance(v, dict) and v.get("ts", 0) > cutoff}
        _save_store(store)


# ------------------------------------------------------------ fire evaluation
def evaluate_fire(alert, now_s=None):
    """Decide whether an alert may fire now.

    Returns (fire: bool, reason: str). May transition the alert
    (expiry, once->triggered, anti-spam auto-stop) as a side effect.
    """
    now_s = now_s if now_s is not None else _now()
    a = _norm_alert(alert)
    if a["status"] != STATUS_ACTIVE:
        return False, "status:" + STATUS_LABEL[a["status"]]
    exp = a.get("expires_at")
    if exp and now_s > int(exp):
        update_alert(a["id"], {"status": STATUS_EXPIRED})
        log_system(a["id"], a["kind"], "expired",
                   "Alert expired: %s" % a["name"])
        return False, "status:" + STATUS_LABEL[STATUS_EXPIRED]
    if a["freq"] == "every" and a["cooldown_min"] > 0 and a["last_triggered_at"]:
        if now_s - int(a["last_triggered_at"]) < a["cooldown_min"] * 60:
            return False, "cooldown"
    # anti-spam: >ANTISPAM_MAX fires inside ANTISPAM_WINDOW -> auto-stop
    recent = [t for t in a["fire_times"] if now_s - t < ANTISPAM_WINDOW]
    if len(recent) >= ANTISPAM_MAX:
        update_alert(a["id"], {"status": STATUS_MANUAL})
        log_system(a["id"], a["kind"], "auto_stopped",
                   "Auto-stopped: triggered %d times in %d seconds: %s"
                   % (len(recent), ANTISPAM_WINDOW, a["name"]))
        return False, "auto_stopped:too_frequent"
    return True, "ok"


def record_fire(alert_id, price=None):
    """Book-keep a fire: counters, last_triggered, once->stopped_triggered."""
    now_s = _now()
    with _lock:
        store = _load_store()
        for i, a in enumerate(store["alerts"]):
            if str(a.get("id")) == str(alert_id):
                na = _norm_alert(a)
                na["last_triggered_at"] = now_s
                na["trigger_count"] = na["trigger_count"] + 1
                ft = [t for t in na["fire_times"] if now_s - t < ANTISPAM_WINDOW]
                ft.append(now_s)
                na["fire_times"] = ft
                if na["freq"] == "once":
                    na["status"] = STATUS_TRIGGERED
                store["alerts"][i] = na
                _save_store(store)
                return na
    return None


# ------------------------------------------------------------------ log
def _load_log():
    d = _read_json(LOG_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("events"), list):
        return d["events"]
    return []


def _save_log(events):
    _write_json(LOG_FILE, {"events": events[:LOG_CAP]})


def log_trigger(alert_id, kind, title, text, price_at_fire=None, delivery=None):
    ev = {
        "id": _uid("e_"),
        "alert_id": alert_id, "kind": kind, "sys": False,
        "title": title, "triggered_at": _now(),
        "price_at_fire": price_at_fire, "text": text or "",
        "delivery": delivery or {},
    }
    with _lock:
        events = _load_log()
        events.insert(0, ev)
        _save_log(events)
    return ev


def log_system(alert_id, kind, action, detail):
    ev = {
        "id": _uid("e_"), "alert_id": alert_id, "kind": kind or "", "sys": True,
        "title": action, "triggered_at": _now(),
        "price_at_fire": None, "text": detail or "", "delivery": {},
    }
    with _lock:
        events = _load_log()
        events.insert(0, ev)
        _save_log(events)
    return ev


def get_log(limit=200, offset=0):
    with _lock:
        events = _load_log()
    total = len(events)
    return {"events": events[offset:offset + limit], "total": total}


def clear_log():
    with _lock:
        _save_log([])


def log_csv():
    import csv
    import io
    import datetime
    # IST has no DST, so a fixed +5:30 offset is exact and needs no tz database.
    _ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    with _lock:
        events = _load_log()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_ist", "kind", "title", "price_at_fire", "telegram", "webhook", "text"])
    for e in events:
        ts = datetime.datetime.fromtimestamp(
            e.get("triggered_at") or 0, tz=_ist).strftime("%Y-%m-%d %H:%M:%S")
        d = e.get("delivery") or {}
        w.writerow([ts, e.get("kind"), e.get("title"), e.get("price_at_fire"),
                    d.get("telegram", ""), d.get("webhook", ""),
                    (e.get("text") or "").replace("\n", " | ")])
    return buf.getvalue()
