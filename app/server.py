"""
StockSophist Charts - local chart server.

Serves:
  /                  -> chart UI (multi-pane, drawings, saved layouts)
  /api/status        -> live/demo mode, detected symbol, clock
  /api/candles       -> OHLC candles, tf = 1s|15s|1m|3m|5m|15m|30m|1H|4H|D|W|M
                       (`upto` unix seconds truncates the series for bar-replay)
  /api/tickbars      -> sub-minute bars (1s/15s) from MT5 tick history
  /api/renko         -> traditional Renko bricks, box = price units (default 2.5)
                       (`upto` rebuilds bricks as of the replay cursor)
  /api/ticks         -> recent tick-by-tick bid/ask
  /api/quote         -> latest bid/ask
  /api/mt5/*         -> connection manager: status / connect / disconnect /
                        detect terminal paths
  /api/indicators    -> indicator catalogue (settings schema)
  /api/indicator/<name> -> computed indicator overlays for a pane
                       (`upto` restricts computation to the replay cursor)

Run:  python app/server.py        (from the chart_app folder)
Then open: http://localhost:5000   (or STOCKSOPHIST_CHART_PORT if set)
"""

import os
import re
import sys
import time
import json
import hashlib
import threading
import webbrowser

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Response, jsonify, make_response, request, send_from_directory  # noqa: E402

from mt5feed import Feed, HAS_MT5  # noqa: E402
from oandafeed import OandaFeed  # noqa: E402
import oandafeed as _oandafeed_mod  # noqa: E402
from mt5conn import MT5Connection, detect_terminals  # noqa: E402
from renko import build_renko, RenkoChain, MAX_TICK_CATCHUP_S  # noqa: E402

# Stable per-(symbol, box) Renko chains for indicator computation.
# A full rebuild re-anchors every brick, so a revised M1 bar would shift the
# whole series and make plotted signals flicker. The chain appends only -
# closed bricks never change.
_RENKO_IND_CHAINS = {}
_RENKO_IND_LOCK = threading.Lock()
# deep sub-minute bar pulls for Renko base series, per
# (symbol, base_tf). One MT5 tick-history pull, then cheap tick-vault
# tail appends (see _renko_base_bars).
_RENKO_BASE = {}
_RENKO_BASE_LOCK = threading.Lock()
_RENKO_BASE_MAX = 30000
# (symbol, base_tf) keys whose first deep tick pull is running in a
# background thread. The candle->Renko mode switch returns {"loading": true}
# immediately instead of blocking the request on the pull.
_RENKO_BASE_LOADING = set()
# background renko pull failures are cached with a timestamp and
# re-raised for 5 minutes (same backoff shape as the gap-fill guard); after
# the backoff a fresh background pull is allowed, so a recovered terminal
# heals without a server restart.
_RENKO_BASE_FAIL_BACKOFF = 300


class _RenkoLoading(RuntimeError):
    """The renko base series is still building in the background.

    Raised (never returned) so every caller is forced to handle the
    transient state: the chart returns {"loading": true}, the poll degrades
    to "no live bricks", the alert engine skips the cycle.
    """
import indicators as indlib  # noqa: E402 (library: built-ins + user scripts)
from mt5trade import register_trade_routes  # noqa: E402 (direct chart trading)
from trade_taker import register_tradetaker_routes  # noqa: E402 (Trade Taker tool)
import alert_defs  # noqa: E402 (alert definitions + trigger log)
from tickstore import TickVault  # noqa: E402 (local 1-second tick vault)
import ops  # noqa: E402

VERSION = "3.4.46"

# import helper - server runs as `python app/server.py` (script mode,
# no parent package, so `from . import x` fails) OR as `python -m app.server`.
# This works in both modes.
import importlib as _il
def _imp(name):
    try:
        return _il.import_module("." + name, __package__ or "app")
    except (ImportError, TypeError, ValueError):
        return _il.import_module(name)

HERE = os.path.dirname(os.path.abspath(__file__))
MT5_CONN_FILE = os.path.join(HERE, "mt5_connection.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")  # telegram + misc (token never leaves server)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"))


# ------------------------------------------------------ JSON error envelope
# The frontend parses every /api/* response as JSON. Flask's default HTML
# error pages would surface as "unexpected token ... is not valid JSON".
# Every API error comes back as {"error": "..."} instead.
@app.errorhandler(404)
def _json_404(e):
    if (request.path or "").startswith("/api/"):
        return jsonify({"error": "not found: " + request.path}), 404
    return e


@app.errorhandler(RuntimeError)
def _json_runtime(e):
    # Our intentional errors ("MT5 is not connected...", "no demo data...") carry
    # the message the user should see - pass it through as JSON, not HTML.
    if (request.path or "").startswith("/api/"):
        return jsonify({"error": str(e) or "request failed"}), 503
    return e


@app.errorhandler(500)
def _json_500(e):
    if (request.path or "").startswith("/api/"):
        return jsonify({"error": "server error on " + request.path}), 500
    return e


# ------------------------------------------------------ local settings file
def _load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def _tg_cfg():
    s = _load_settings()
    tg = s.get("telegram") or {}
    return {
        # default ON when the key is absent (user's standing choice);
        # an explicit false is still honored.
        "enabled": bool(tg.get("enabled", True)),
        "token": tg.get("token") or "",
        "chat_id": tg.get("chat_id") or "",
        "on_price": tg.get("on_price", True),
        "on_sweep": tg.get("on_sweep", True),
    }


def _webhook_cfg():
    s = _load_settings()
    wh = s.get("webhook") or {}
    return {
        "enabled": bool(wh.get("enabled")),
        "url": (wh.get("url") or "").strip(),
    }


def _post_json(url, payload, timeout=10):
    """Server-side JSON POST. Raises on any failure - callers decide."""
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def _fire_global_webhook(event, symbol, price, text):
    """Outbound webhook for chart alerts. Independent of Telegram: enabled +
    URL in settings is all it needs. Failures only log, never raise."""
    c = _webhook_cfg()
    if not c["enabled"] or not c["url"]:
        return {"sent": False, "skipped": "webhook not configured"}
    payload = {
        "event": event,               # "price_alert" | "sweep" | "test"
        "symbol": symbol or "",
        "price": price,
        "text": text or "",
        "time": int(time.time()),
    }
    try:
        _post_json(c["url"], payload, timeout=10)
        return {"sent": True}
    except Exception as e:
        print("global webhook POST failed: %s" % e, flush=True)
        return {"sent": False, "error": str(e)}


# ------------------------------------------------------------ telegram
@app.route("/api/telegram/config")
def api_tg_config():
    """Saved Telegram settings. The token is NEVER returned - only has_token."""
    c = _tg_cfg()
    return jsonify({
        "enabled": c["enabled"],
        "has_token": bool(c["token"]),
        "chat_id": c["chat_id"],
        "on_price": c["on_price"],
        "on_sweep": c["on_sweep"],
    })


@app.route("/api/telegram/config", methods=["POST"])
def api_tg_config_save():
    """Save Telegram settings. Blank token = keep the stored one."""
    body = request.get_json(force=True, silent=True) or {}
    s = _load_settings()
    tg = dict((s.get("telegram") or {}))
    if "enabled" in body:
        tg["enabled"] = bool(body["enabled"])
    if "chat_id" in body:
        tg["chat_id"] = (body["chat_id"] or "").strip()
    if "on_price" in body:
        tg["on_price"] = bool(body["on_price"])
    if "on_sweep" in body:
        tg["on_sweep"] = bool(body["on_sweep"])
    tok = (body.get("token") or "").strip()
    if tok:  # blank = keep stored token
        tg["token"] = tok
    s["telegram"] = tg
    try:
        _save_settings(s)
    except Exception as e:
        return jsonify({"error": "could not save settings: " + str(e)}), 500
    return jsonify({"ok": True, "has_token": bool(tg.get("token"))})


@app.route("/api/feed/config")
def api_feed_config():
    """Data-feed settings. The OANDA token is NEVER returned - only has_token.
    Changing data_source or the OANDA credentials requires an app restart
    (the feed object is created at startup)."""
    s = _load_settings()
    oc = s.get("oanda") or {}
    return jsonify({
        "data_source": (s.get("data_source") or "mt5").strip().lower(),
        "active_source": _FEED_TAG_RAW,
        "oanda": {
            "has_token": bool(oc.get("token")),
            "account_id": oc.get("account_id") or "",
        },
    })


@app.route("/api/feed/config", methods=["POST"])
def api_feed_config_save():
    """Save data-feed settings. Blank token = keep the stored one.
    Takes effect on restart - the response says so explicitly."""
    body = request.get_json(force=True, silent=True) or {}
    s = _load_settings()
    if "data_source" in body:
        ds = (body["data_source"] or "").strip().lower()
        if ds not in ("mt5", "oanda"):
            return jsonify({"ok": False,
                            "error": "data_source must be 'mt5' or 'oanda'"}), 400
        s["data_source"] = ds
    oc = dict((s.get("oanda") or {}))
    if "account_id" in body:
        oc["account_id"] = (body["account_id"] or "").strip()
    tok = (body.get("token") or "").strip()
    if tok:  # blank = keep stored token
        oc["token"] = tok
    s["oanda"] = oc
    try:
        _save_settings(s)
    except Exception as e:
        return jsonify({"error": "could not save settings: " + str(e)}), 500
    return jsonify({"ok": True, "has_token": bool(oc.get("token")),
                    "restart_required": True,
                    "message": "Saved. Restart the chart app to switch the data feed."})


@app.route("/api/telegram/send", methods=["POST"])
def api_tg_send():
    """Alert dispatch: Telegram + the global outbound webhook.

    Body: {text, kind ("price"|"sweep"|""), skip_webhook, symbol, price}.
    The webhook is independent of Telegram - it fires whenever it is enabled
    with a URL, even if Telegram is off or fails. Webhook failures only log.
    skip_webhook is honoured (the per-indicator alert path sets it when it
    already posted the same payload to the same URL).
    """
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    kind = (body.get("kind") or "").strip()  # "price" | "sweep" | ""
    skip_webhook = bool(body.get("skip_webhook"))
    symbol = (body.get("symbol") or "").strip()
    price = body.get("price")
    if not text:
        return jsonify({"error": "text required"}), 400

    event = {"price": "price_alert", "sweep": "sweep"}.get(kind, "test")
    webhook = ({"sent": False, "skipped": "skip_webhook"}
               if skip_webhook
               else _fire_global_webhook(event, symbol, price, text))

    c = _tg_cfg()
    telegram = {"sent": False}
    if not c["enabled"] or not c["token"] or not c["chat_id"]:
        telegram["skipped"] = "Telegram not configured (enable it and set token + chat id)"
    elif kind == "price" and not c["on_price"]:
        telegram["skipped"] = "price alerts off"
    elif kind == "sweep" and not c["on_sweep"]:
        telegram["skipped"] = "sweep alerts off"
    else:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.telegram.org/bot" + c["token"] + "/sendMessage",
                data=json.dumps({"chat_id": c["chat_id"], "text": text,
                                 "disable_web_page_preview": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                telegram["sent"] = 200 <= resp.status < 300
        except Exception as e:
            telegram["error"] = "Telegram send failed: " + str(e)
    return jsonify({"ok": True, "telegram": telegram, "webhook": webhook})


@app.route("/api/webhook/config")
def api_webhook_config():
    """Global outbound webhook settings (URL never leaves the server)."""
    c = _webhook_cfg()
    return jsonify({"enabled": c["enabled"], "url": c["url"],
                    "has_url": bool(c["url"])})


@app.route("/api/webhook/config", methods=["POST"])
def api_webhook_config_save():
    """Save the global outbound webhook. Blank URL = keep the stored one."""
    body = request.get_json(force=True, silent=True) or {}
    s = _load_settings()
    wh = dict((s.get("webhook") or {}))
    if "enabled" in body:
        wh["enabled"] = bool(body["enabled"])
    url = (body.get("url") or "").strip()
    if url:  # blank = keep stored URL
        wh["url"] = url
    s["webhook"] = wh
    try:
        _save_settings(s)
    except Exception as e:
        return jsonify({"error": "could not save settings: " + str(e)}), 500
    return jsonify({"ok": True, "enabled": bool(wh.get("enabled")),
                    "has_url": bool((wh.get("url") or "").strip())})


@app.route("/api/webhook/fire", methods=["POST"])
def api_webhook_fire():
    """Outbound webhook for per-indicator alerts (the ... alert dialog).

    Body: {url (optional - falls back to the saved global webhook URL),
           text, symbol, price, indicator, event}.
    The POST is done server-side so browser CORS is never an issue and the
    URL never has to be exposed to the page.
    """
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        url = ((_load_settings().get("webhook") or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "no webhook URL (set one in the alert dialog)"}), 400
    payload = {
        "event": body.get("event") or "indicator_alert",
        "indicator": body.get("indicator") or "",
        "symbol": body.get("symbol") or "",
        "price": body.get("price"),
        "text": body.get("text") or "",
        "time": int(time.time()),
    }
    try:
        ok = _post_json(url, payload, timeout=10)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": "Webhook POST failed: " + str(e)}), 502


# --------------------------------------------------------------- alerts: definitions + log
# Alert manager: server-owned definitions with lifecycle
# states, a persistent trigger log, and ONE fire path for every alert kind.
@app.get("/api/alerts")
def api_alerts_list():
    return jsonify({"alerts": alert_defs.list_alerts()})


@app.post("/api/alerts")
def api_alerts_create():
    body = request.get_json(force=True, silent=True) or {}
    a = alert_defs.create_alert(body)
    return jsonify({"ok": True, "alert": a})


@app.put("/api/alerts/<alert_id>")
def api_alerts_update(alert_id):
    body = request.get_json(force=True, silent=True) or {}
    a = alert_defs.update_alert(alert_id, body)
    if not a:
        return jsonify({"error": "alert not found"}), 404
    return jsonify({"ok": True, "alert": a})


@app.delete("/api/alerts/<alert_id>")
def api_alerts_delete(alert_id):
    if not alert_defs.delete_alert(alert_id):
        return jsonify({"error": "alert not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/alerts/<alert_id>/stop")
def api_alerts_stop(alert_id):
    a = alert_defs.set_status(alert_id, alert_defs.STATUS_MANUAL)
    if not a:
        return jsonify({"error": "alert not found"}), 404
    return jsonify({"ok": True, "alert": a})


@app.post("/api/alerts/<alert_id>/resume")
def api_alerts_resume(alert_id):
    a = alert_defs.set_status(alert_id, alert_defs.STATUS_ACTIVE)
    if not a:
        return jsonify({"error": "alert not found"}), 404
    return jsonify({"ok": True, "alert": a})


@app.get("/api/alerts/log")
def api_alerts_log():
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 200))))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0
    try:
        since = int(float(request.args.get("since", 0) or 0))
    except Exception:
        since = 0
    data = alert_defs.get_log(limit, offset)
    if since:
        data["events"] = [e for e in data["events"]
                          if int(e.get("triggered_at") or 0) > since]
    return jsonify(data)


@app.delete("/api/alerts/log")
def api_alerts_log_clear():
    alert_defs.clear_log()
    return jsonify({"ok": True})


@app.get("/api/alerts/log.csv")
def api_alerts_log_csv():
    csv_text = alert_defs.log_csv()
    resp = make_response(csv_text)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=stocksophist_alerts_log.csv"
    return resp


# ------------------------------------------------custom alert tone
# Server-side storage for the user's custom alert tune. The file lives in
# DATA_DIR/tones/ (outside the code tree) so it survives delete-and-replace
# updates; the browser copy in localStorage is legacy fallback only.
_TONE_EXTS = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
              "m4a": "audio/mp4", "flac": "audio/flac"}
_TONE_MAX_BYTES = 10 * 1024 * 1024


def _tone_dir():
    # Lazy: DATA_DIR is defined further down this module; requests only
    # arrive after the module fully loads.
    d = os.path.join(DATA_DIR, "tones")
    return d


def _tone_path():
    try:
        for fn in os.listdir(_tone_dir()):
            if fn.startswith("custom."):
                return os.path.join(_tone_dir(), fn)
    except Exception:
        pass
    return None


@app.route("/api/alerts/tone", methods=["POST"])
def api_alerts_tone_upload():
    """Upload a custom alert tune. Multipart field `file`; validated and
    stored as DATA_DIR/tones/custom.<ext> (replaces any previous custom)."""
    f = request.files.get("file")
    if f is None or not (f.filename or "").strip():
        return jsonify({"error": "no file uploaded"}), 400
    name = os.path.basename(f.filename.strip())
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _TONE_EXTS:
        return jsonify({"error":
                        "unsupported file type - use mp3, wav, ogg, m4a or flac"}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "empty file"}), 400
    if len(data) > _TONE_MAX_BYTES:
        return jsonify({"error": "file too large - 10 MB max"}), 400
    try:
        os.makedirs(_tone_dir(), exist_ok=True)
        old = _tone_path()
        if old:
            try:
                os.remove(old)
            except Exception:
                pass
        dest = os.path.join(_tone_dir(), "custom." + ext)
        with open(dest, "wb") as fh:
            fh.write(data)
    except Exception as e:
        return jsonify({"error": "could not save tune: %s" % str(e)[:100]}), 500
    return jsonify({"ok": True})


@app.route("/api/alerts/tone", methods=["GET", "HEAD"])
def api_alerts_tone_get():
    """Serve the stored custom tune, else 404 (JSON envelope)."""
    p = _tone_path()
    if not p or not os.path.exists(p):
        return jsonify({"error": "no custom tune uploaded"}), 404
    ext = p.rsplit(".", 1)[-1].lower()
    if request.method == "HEAD":
        resp = make_response(b"")
        resp.headers["Content-Type"] = _TONE_EXTS.get(ext, "audio/mpeg")
        resp.headers["Content-Length"] = str(os.path.getsize(p))
        return resp
    return send_from_directory(_tone_dir(), os.path.basename(p),
                               mimetype=_TONE_EXTS.get(ext, "audio/mpeg"))


def _apply_message_template(alert, text, price, symbol):
    tpl = (alert.get("message") or "{{text}}")
    out = tpl.replace("{{text}}", text or "")
    out = out.replace("{{price}}", "" if price is None else str(price))
    out = out.replace("{{symbol}}", symbol or "")
    out = out.replace("{{name}}", alert.get("name") or "")
    return out


def _send_own_telegram(text, kind=""):
    """Send one of the app's OWN alerts directly to the configured Telegram
    chat (settings "telegram": bot token + target chat id). No relay, no
    channels, no intermediate hop. Returns "sent" / "skipped:<reason>" /
    "failed:<error>". kind: "" (indicator) | "price" - honours the
    per-kind toggles in Telegram settings."""
    c = _tg_cfg()
    if not c["enabled"] or not c["token"] or not c["chat_id"]:
        return "skipped:Telegram not configured (enable it and set token + chat id)"
    if kind == "price" and not c["on_price"]:
        return "skipped:price alerts off"
    if kind == "sweep" and not c["on_sweep"]:
        return "skipped:sweep alerts off"
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.telegram.org/bot" + c["token"] + "/sendMessage",
            data=json.dumps({"chat_id": c["chat_id"], "text": text,
                             "disable_web_page_preview": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return "sent" if 200 <= resp.status < 300 else "failed:http %s" % resp.status
    except Exception as e:
        return "failed:Telegram send failed: %s" % str(e)


def _post_webhook(url, kind, symbol, price, text):
    """POST an alert JSON to a user-supplied webhook URL. Never raises."""
    import urllib.request
    payload = json.dumps({
        "event": kind, "symbol": symbol, "price": price,
        "text": text, "time": int(time.time()),
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "StockSophistCharts/3.4.46"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = getattr(resp, "status", 200)
        return "sent:%s" % code
    except Exception as e:
        return "failed:%s" % str(e)[:120]


def _fire_indicator_alert(alert, indicator_id, indicator_name, symbol, tf,
                          text, price, sweep_key, is_event, settings=None,
                          chart_mode="", box=0, signal_time=None):
    """Shared fire logic for indicator signals.

    Used by the /api/alerts/fire route (browser-side instant fires) AND by
    the background Alert Engine . Exactly one of them wins per signal
    because both go through the same sweep-key dedup + lifecycle evaluation.

    when the alert has an explicit `timeframe` and the caller reports
    a different TF, the fire is refused with "tf_mismatch" - the engine owns
    evaluation on the alert's timeframe, so a pane on another TF can neither
    double-fire nor fire the wrong TF's signals.

    `chart_mode`/`box` stamp a Renko-pane origin on the alert
    definition so the engine evaluates it on bricks, not candles.

    `signal_time` (bar time of the signal, when the caller knows
    it - engine event path: sweep `t`; level path: first scanned bar
    touching the level) keeps evaluation as-of the SIGNAL bar, not
    as-of now. Browser route omits it: evaluation falls back to the
    latest closed bar.
    """
    if not alert:
        alert = alert_defs.ensure_indicator_alert(
            indicator_id, indicator_name, symbol,
            timeframe=tf, settings=settings,
            chart_mode=chart_mode, box=box)
    alert_tf = (alert.get("timeframe") or "").strip()
    alert_mode = (alert.get("chart_mode") or "").strip()
    if alert_mode == "renko":
        # A Renko alert is evaluated on bricks (engine-owned). A candle-pane
        # fire for the same indicator must neither double-fire nor misfire.
        try:
            alert_box = float(alert.get("box") or 0)
        except (TypeError, ValueError):
            alert_box = 0
        try:
            fire_box = float(box or 0)
        except (TypeError, ValueError):
            fire_box = 0
        if chart_mode != "renko" or abs(fire_box - alert_box) > 1e-9:
            return {"fired": False, "reason": "tf_mismatch", "alert": alert,
                    "detail": "alert evaluates on Renko bricks (box %s)" % alert_box}
        # bricks are built from the alert's timeframe - a browser
        # fire from a Renko pane on a different TF sees different bricks,
        # so it must not fire this alert; the engine owns that TF.
        if alert_tf and tf and tf != alert_tf:
            return {"fired": False, "reason": "tf_mismatch", "alert": alert,
                    "detail": "alert evaluates on Renko bricks from %s bars" % alert_tf}
    else:
        if chart_mode == "renko":
            return {"fired": False, "reason": "tf_mismatch", "alert": alert,
                    "detail": "alert evaluates on candles; this pane is Renko"}
        if alert_tf and tf and tf != alert_tf:
            return {"fired": False, "reason": "tf_mismatch", "alert": alert,
                    "detail": "alert evaluates on %s" % alert_tf}
    if sweep_key and alert_defs.sweep_already_fired(sweep_key):
        return {"fired": False, "reason": "duplicate", "alert": alert}
    fire, reason = alert_defs.evaluate_fire(alert)
    if not fire:
        alert_defs.log_system(
            alert["id"], "indicator", "skipped",
            "Skipped (%s): %s - %s" % (reason, alert["name"], (text or "")[:120]))
        return {"fired": False, "reason": reason, "alert": alert}
    # own-alert text goes out verbatim (no relay keyword shaping).
    msg = _apply_message_template(alert, text, price, symbol)
    delivery = {}
    notify = alert.get("notify") or {}
    if notify.get("telegram", True):
        delivery["telegram"] = _send_own_telegram(msg)
    else:
        delivery["telegram"] = "disabled"
    if notify.get("webhook", True) and alert.get("webhook_url"):
        delivery["webhook"] = _post_webhook(
            alert["webhook_url"], "indicator", symbol, price, msg)
    else:
        delivery["webhook"] = "skipped" if alert.get("webhook_url") else "disabled"
    alert_defs.record_fire(alert["id"], price)
    if sweep_key:
        alert_defs.mark_sweep_fired(sweep_key, alert["id"])
    ev = alert_defs.log_trigger(alert["id"], "indicator", alert["name"],
                                msg, price, delivery)
    return {"fired": True, "reason": "ok", "alert": alert,
            "delivery": delivery, "event_id": ev["id"], "notify": notify}


def _fire_price_alert(alert, price):
    """Shared fire logic for price alerts (route + Alert Engine)."""
    fire, reason = alert_defs.evaluate_fire(alert)
    if not fire:
        return {"fired": False, "reason": reason, "alert": alert}
    symbol = alert.get("symbol") or ""
    cond = alert.get("condition") or "crossing"
    base = "Price Alert: %s %s %s" % (symbol, cond, price)
    if alert.get("note"):
        base += " - " + alert["note"]
    msg = _apply_message_template(alert, base, price, symbol)
    delivery = {}
    notify = alert.get("notify") or {}
    if notify.get("telegram", True):
        delivery["telegram"] = _send_own_telegram(msg, kind="price")
    else:
        delivery["telegram"] = "disabled"
    if notify.get("webhook", True) and alert.get("webhook_url"):
        delivery["webhook"] = _post_webhook(
            alert["webhook_url"], "price", symbol, price, msg)
    else:
        delivery["webhook"] = "skipped" if alert.get("webhook_url") else "disabled"
    na = alert_defs.record_fire(alert["id"], price)
    ev = alert_defs.log_trigger(alert["id"], "price", alert["name"], msg, price, delivery)
    return {"fired": True, "reason": "ok", "alert": na or alert,
            "delivery": delivery, "event_id": ev["id"], "notify": notify}


@app.route("/api/alerts/fire", methods=["POST"])
def api_alerts_fire():
    """Single fire path for price alerts and indicator signals.

    Price:     {"alert_id": ..., "price": ...}
    Indicator: {"kind": "indicator", "indicator_id": ..., "indicator_name": ...,
                "symbol": ..., "tf": ..., "text": ..., "price": ...,
                "sweep_key": ..., "event": bool, "settings": {...},
                "chart_mode": "candles"|"renko", "box": float}
    """
    body = request.get_json(force=True, silent=True) or {}
    kind = body.get("kind") or "price"

    if kind == "indicator":
        indicator_id = str(body.get("indicator_id") or "")
        indicator_name = str(body.get("indicator_name") or indicator_id)
        symbol = str(body.get("symbol") or "")
        pane_tf = str(body.get("tf") or "")
        price = body.get("price")
        sweep_key = str(body.get("sweep_key") or "")
        raw_text = str(body.get("text") or "")
        is_event = bool(body.get("event"))
        settings = body.get("settings") if isinstance(body.get("settings"), dict) else None
        chart_mode = str(body.get("chart_mode") or "")
        try:
            box = float(body.get("box") or 0)
        except (TypeError, ValueError):
            box = 0
        if not indicator_id:
            return jsonify({"error": "indicator_id required"}), 400
        res = _fire_indicator_alert(None, indicator_id, indicator_name, symbol,
                                    pane_tf, raw_text, price, sweep_key,
                                    is_event, settings=settings,
                                    chart_mode=chart_mode, box=box)
        return jsonify(res)

    # --- price alert
    alert_id = body.get("alert_id")
    price = body.get("price")
    alert = alert_defs.get_alert(alert_id) if alert_id else None
    if not alert:
        return jsonify({"error": "alert not found"}), 404
    return jsonify(_fire_price_alert(alert, price))

# Disk persistence: deep history survives restarts, so TF switches and
# app reopens are instant and long absences backfill only the missing tail.
# The cache lives OUTSIDE the app folder (C:\stocksophist\cache on Windows)
# so it survives the user's delete-and-replace update workflow.
if os.name == "nt":
    DATA_DIR = r"C:\stocksophist\cache"
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".stocksophist_chart_cache")
os.makedirs(DATA_DIR, exist_ok=True)
PARQUET_DIR = os.path.join(DATA_DIR, "parquet")

# full code/data separation - EVERY runtime-written file lives under
# DATA_DIR (outside the code tree). Delete-and-replace updates only swap the
# code folder; user data (settings, alerts, tones, caches, favorites, custom
# indicators) survives untouched and the user can back the data dir up alone.
# this block runs BEFORE the feed is created, so _make_feed() and
# OandaFeed read the migrated settings - never the (possibly wiped) in-tree copy.
import shutil as _shutil  # noqa: E402


def _migrate_to_data_dir(legacy_path, name):
    """Copy an in-tree runtime file into DATA_DIR."""
    new_path = os.path.join(DATA_DIR, name)
    if not os.path.exists(new_path) and os.path.exists(legacy_path):
        try:
            _shutil.copy2(legacy_path, new_path)
            print("[chart] migrated %s to data dir" % name)
        except Exception as _e:
            print("[chart] migration of %s failed: %s" % (name, _e))
    return new_path


# settings.json (OANDA token + all settings) - zero re-entry after an update.
SETTINGS_FILE = _migrate_to_data_dir(
    os.path.join(HERE, "settings.json"), "settings.json")
# MT5 connection config (the "remember" path) - same treatment.
MT5_CONN_FILE = _migrate_to_data_dir(
    os.path.join(HERE, "mt5_connection.json"), "mt5_connection.json")
# OANDA feed reads its token/account from the same (migrated) settings file.
_oandafeed_mod.set_settings_path(SETTINGS_FILE)

# Robust MT5 connection: attach to a running terminal / launch at the saved
# path / log in to the saved account, with a background watchdog that repairs
# silent broker-session drops. Data methods only go live when this says so.
conn = MT5Connection(MT5_CONN_FILE)
conn.config_path = MT5_CONN_FILE
conn._load()

# data-source switch. settings.json -> {"data_source": "oanda"}
# (default "mt5") + {"oanda": {"token": ..., "account_id": ...}}.
# OANDA mode serves XAUUSD from OANDA's free practice feed (REST history +
# tick-by-tick price stream); MT5 mode is unchanged. The two feeds never
# share cache files - _FEED_TAG namespaces every parquet path below.
def _make_feed():
    try:
        ds = (_load_settings().get("data_source") or "mt5").strip().lower()
    except Exception:
        ds = "mt5"
    if ds == "oanda":
        return OandaFeed(), "oanda"
    return Feed(parquet_dir=PARQUET_DIR, conn=conn), "mt5"


feed, _FEED_TAG_RAW = _make_feed()
_FEED_TAG = ".oanda" if _FEED_TAG_RAW == "oanda" else ""
_FEED_NAME = "OANDA" if _FEED_TAG_RAW == "oanda" else "MT5"

# If MT5 is available, attach to the already-running terminal at startup so the
# chart is live without opening the MT5 dialog first. Runs in the background;
# the server still starts in DEMO if no terminal answers. Set
# STOCKSOPHIST_NO_AUTO_MT5=1 to skip the attempt entirely.
if HAS_MT5 and os.environ.get("STOCKSOPHIST_NO_AUTO_MT5") != "1":
    conn.connect_async()

# Direct chart trading (BUY/SELL at market, pendings, drag SL/TP, close).
# Every route refuses when MT5 is not live - DEMO can never send an order.
register_trade_routes(app, conn)
register_tradetaker_routes(app, conn)

# Deep history targets (bars per timeframe).
# The first load backfills up to this many bars; later polls only refresh
# the tail, so panning back stays instant.
_DEEP_BARS = {
    "1s": 7200, "15s": 5760,                       # bounded by broker tick depth
    "1m": 20000, "3m": 20000, "5m": 20000,
    "15m": 20000, "30m": 20000, "1H": 20000, "4H": 20000,
    "D": 5000, "W": 2000, "M": 600,
}
_DEEP = {}            # (symbol, tf) -> {"bars": [...]}
_DEEP_LOCK = threading.Lock()

# DATA_DIR + settings migration now run BEFORE the feed is created
# (see above, ahead of `conn = MT5Connection(...)`); the remaining runtime-dir
# bindings stay here, after the separation point.
# backfill runtime dirs: incr / ticks / backups (+ csv-import + incr-merge).
_imp("backfill").set_runtime_dir(DATA_DIR)
# Price-alert definitions + trigger log.
alert_defs.set_data_dir(DATA_DIR)
# Indicator user data: favorites.json + "My scripts" custom indicators.
indlib.set_runtime_dir(os.path.join(DATA_DIR, "indicators"))

# Local tick vault: every MT5 quote the server sees is recorded as 1-second
# OHLC in DATA_DIR/ticks.db - by the /api/quote hook AND by a background
# recorder thread (so it stays complete even with no browser tab open). When
# MT5's own rate history freezes or skips chunks while ticks keep flowing,
# gaps are healed from this vault instead of leaving blank chart space.
vault = TickVault(DATA_DIR)
threading.Thread(target=vault.prune, daemon=True).start()
_VAULT_SYMBOLS = set()   # symbols actually polled -> the recorder watches them

_TF_SEC = {"1s": 1, "15s": 15, "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
           "1H": 3600, "4H": 14400, "D": 86400, "W": 604800, "M": 2592000}


def _disk_path(symbol, tf):
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in symbol)
    # OANDA and MT5 never share cache files (different price feeds).
    return os.path.join(DATA_DIR, "%s_%s%s.parquet" % (safe, tf, _FEED_TAG))


# Instant start: indicator result disk cache.
# Computing an indicator on a cold start costs a full _deep_history plus
# the compute itself (seconds each - worse on a slow terminal), and every
# pane refetches its indicators on every browser load. The result only
# changes when (a) bars advance, (b) old bars get repaired, or (c) the
# settings change, so we cache the last JSON result on disk, keyed by
# indicator+symbol+tf+mode+box+settings+replay-cursor and validated
# against the disk bars' (count, last_time, mtime, size) signature -
# computed from the disk cache only, never touching MT5. On a restart the
# overlays paint instantly from cache; the normal request path recomputes
# when the signature moves and refreshes the cache.
_IND_CACHE_DIR = os.path.join(DATA_DIR, "indicator_cache")
_IND_CACHE_MAX = 120  # LRU cap on cached result files


def _ind_cache_key(ind_id, symbol, tf, mode, box, settings, upto):
    try:
        s = json.dumps(settings or {}, sort_keys=True, default=str)
    except Exception:
        s = ""
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
    safe = lambda v: "".join(
        c if (c.isalnum() or c in "-_.") else "_" for c in str(v))[:48]
    return "ind_%s_%s_%s_%s_%s_%s_%d" % (
        safe(ind_id), safe(symbol), safe(tf), safe(mode),
        safe(box), h, int(upto or 0))


def _ind_cache_sig(symbol, tf):
    """Cheap signature of the disk bars: (count, last_time, rev).

    No MT5, no deep history, no full bar materialization - only the time
    column is read. count/last_time catch appended data; rev (the .rev
    sidecar maintained by _save_disk) counts real content changes and
    catches in-place repairs of old bars (glitch repair, volume healing)
    that change neither count nor last_time, so those never serve a stale
    indicator result. The .basis check mirrors _load_disk.

    rev replaces the old (mtime_ns, size) pair - _save_disk now
    skips the rewrite when content is unchanged, so mtime does not move
    on every _deep_history call and the cache actually hits.
    """
    try:
        p = _disk_path(symbol, tf)
        try:
            with open(p + ".basis", encoding="utf-8") as f:
                if f.read().strip() != _DISK_BASIS:
                    return (0, 0, 0)
        except OSError:
            return (0, 0, 0)
        tcol = pd.read_parquet(p, columns=["time"])
        n = len(tcol)
        if n == 0:
            return (0, 0, 0)
        rev = 0
        try:
            with open(p + ".rev", encoding="utf-8") as f:
                rev = int(json.load(f).get("rev", 0))
        except Exception:
            pass
        return (n, int(tcol["time"].max()), rev)
    except Exception:
        return (0, 0, 0)


def _ind_cache_get(key, sig):
    p = os.path.join(_IND_CACHE_DIR, key + ".json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            wrap = json.load(f)
        if not isinstance(wrap, dict):
            return None
        if wrap.get("sig") != list(sig):
            return None
        return wrap.get("result")
    except Exception:
        return None


def _ind_cache_put(key, sig, result):
    try:
        os.makedirs(_IND_CACHE_DIR, exist_ok=True)
        p = os.path.join(_IND_CACHE_DIR, key + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sig": list(sig), "result": result}, f)
        os.replace(tmp, p)
        try:
            files = [(os.path.getmtime(os.path.join(_IND_CACHE_DIR, n)), n)
                     for n in os.listdir(_IND_CACHE_DIR)
                     if n.endswith(".json")]
            files.sort()
            for _, n in files[:-_IND_CACHE_MAX]:
                try:
                    os.remove(os.path.join(_IND_CACHE_DIR, n))
                except OSError:
                    pass
        except OSError:
            pass
    except Exception:
        pass


# bar timestamps are true UTC. Caches written by older builds hold
# MT5 server-basis times (hours in the future); mixing them with UTC bars
# would duplicate/corrupt history. The sidecar marks the basis - a missing
# or stale marker means "unusable, backfill fresh".
_DISK_BASIS = "utc2"  # bumped -and older cached DB-aggregated
# 4H/D bars with UTC-midnight alignment ("ghost candles" vs MT5's
# server-midnight bars). Old caches are ignored, fresh MT5-native data loads.


def _load_disk(symbol, tf):
    p = _disk_path(symbol, tf)
    if not os.path.exists(p):
        return None
    try:
        basis_path = p + ".basis"
        if not os.path.exists(basis_path):
            return None  # pre-UTC cache: server-basis times, must not mix
        with open(basis_path, encoding="utf-8") as f:
            if f.read().strip() != _DISK_BASIS:
                return None
        df = pd.read_parquet(p)
        has_vol = "volume" in df.columns
        has_flags = "flags" in df.columns  # bar provenance
        bars = []
        for r in df.itertuples():
            v = 0
            if has_vol:
                try:
                    vv = float(r.volume)
                    v = int(vv) if vv == vv else 0
                except (TypeError, ValueError):
                    v = 0
            fl = 0
            if has_flags:
                try:
                    fl = int(r.flags or 0)
                except (TypeError, ValueError):
                    fl = 0
            d = {"time": int(r.time), "open": float(r.open),
                 "high": float(r.high), "low": float(r.low),
                 "close": float(r.close), "volume": v}
            if fl & 1:
                d["tickbuilt"] = True
            if fl & 2:
                d["local"] = True
            bars.append(d)
        return sorted(bars, key=lambda b: b["time"])
    except Exception:
        return None


def _disk_fingerprint(df):
    """Cheap content fingerprint of the bars about to be written.

    (count, min_time, max_time, sum_close, sum_open, sum_range,
    sum_volume, sum_flags). Sums are order-independent and catch in-place
    repairs of old bars (glitch repair, volume healing) that change neither
    count nor last_time. Volume is included so's volume healing
    still persists to disk. open prices and the provenance flags
    (tickbuilt/local) are included too - a provenance-only or open-only
    change must not be misclassified as "unchanged".
    """
    try:
        n = len(df)
        if n == 0:
            return "empty"
        tmin = int(df["time"].min())
        tmax = int(df["time"].max())
        sc = round(float(df["close"].sum()), 4)
        so = round(float(df["open"].sum()), 4)
        sr = round(float((df["high"] - df["low"]).sum()), 4)
        sv = int(df["volume"].sum()) if "volume" in df.columns else 0
        sf = int(df["flags"].sum()) if "flags" in df.columns else 0
        return "%d:%d:%d:%s:%s:%s:%d:%d" % (n, tmin, tmax, sc, so, sr, sv, sf)
    except Exception:
        return None


def _save_disk(symbol, tf, bars):
    try:
        # the hot working file is capped - the full max-depth
        # series lives in the .archive.parquet (deep sync job), never here.
        if len(bars) > _WORKING_BARS:
            bars = bars[-_WORKING_BARS:]
        for b in bars:
            b.setdefault("volume", 0)
        # persist bar provenance so reconstructions (tick-built /
        # vault bars) stay re-verifiable after a restart - MT5's rates
        # supersede them once the terminal syncs (glitch repair).
        flags = [(1 if b.get("tickbuilt") else 0) | (2 if b.get("local") else 0)
                 for b in bars]
        df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close",
                                         "volume"])
        df["flags"] = flags
        p = _disk_path(symbol, tf)
        # skip the rewrite when the content is unchanged. Rewriting
        # the parquet on every _deep_history call bumps mtime -
        # which defeats the indicator result cache (its signature never
        # matches) and forces a full indicator recompute on every poll.
        # A .rev sidecar counts only real content changes; the indicator
        # cache keys on (count, last_time, rev) instead of mtime.
        fp = _disk_fingerprint(df)
        prev_rev, prev_fp = 0, None
        try:
            with open(p + ".rev", encoding="utf-8") as f:
                prev = json.load(f)
            prev_rev = int(prev.get("rev", 0))
            prev_fp = prev.get("fp")
        except Exception:
            pass
        if fp is not None and prev_fp is not None and fp == prev_fp:
            return
        df.to_parquet(p, index=False)
        with open(p + ".basis", "w", encoding="utf-8") as f:
            f.write(_DISK_BASIS)
        try:
            tmp = p + ".rev.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rev": prev_rev + 1, "fp": fp}, f)
            os.replace(tmp, p + ".rev")
        except Exception:
            pass
    except Exception:
        pass

# tf -> minutes multiplier for M1 resampling (None = native MT5 TF / tickbars)
_TF_M1_MULT = {"1m": 1, "1": 1, "3m": 3, "3": 3, "5m": 5, "5": 5}
_TF_NATIVE = {"15m", "30m", "1H", "4H", "D", "W", "M"}


# Max-depth history ("deep archive") + Renko brick cache.
# The interactive pipeline (chart polls, indicators, alerts) works off a
# small WORKING SET - the newest _WORKING_BARS bars in {sym}_{tf}.parquet -
# which stays fast to read and cheap to rewrite every minute. The deep
# archive holds everything MT5 can give: {sym}_1m.archive.parquet plus a
# tiny .meta sidecar (bars/first/last), so the incremental check on every
# restart never reads the big file.
# A single background worker walks BACKWARD from the archive's oldest bar
# with copy_rates_range chunks until the broker's history is exhausted,
# then merges - each restart only fetches what's missing (the tail gap,
# plus older bars if the broker has since exposed more). Progress goes to
# the ops bus (op "deephist:{symbol}"), so the UI narrates it.
# Renko brick caches are built from the same archive
# (renko/{sym}_{box}_{base_tf}.parquet + .meta). A cold Renko pane seeds
# its chain from the cache instantly instead of rebuilding from millions
# of bars - candle<->Renko switching stays fast after the first build.
_WORKING_BARS = 200000    # working-set cap for the hot {sym}_{tf}.parquet
_IND_WINDOW_BARS = 50000  # candle-mode indicators compute on the newest 50k
_DEEP_CHUNK_BARS = 100000  # one copy_rates_range call per backward step
_DEEP_MAX_BARS = 8000000   # sanity cap (~15y of M1); the broker exhausts first

_DEEP_SYNC_QUEUE = []
_DEEP_SYNC_LOCK = threading.Lock()
_DEEP_SYNC_ACTIVE = set()  # symbols queued or syncing right now
_BRICK_WANT = set()        # (symbol, box, base_tf) the client asked to cache


def _archive_path(symbol):
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in symbol)
    # feed-namespaced (XAUUSD_1m.archive.oanda.parquet vs .parquet).
    return os.path.join(DATA_DIR, "%s_1m.archive%s.parquet" % (safe, _FEED_TAG))


def _archive_meta(symbol):
    """Fast archive stats without reading the big parquet (None if absent)."""
    try:
        with open(_archive_path(symbol) + ".meta", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_archive_meta(symbol, df):
    meta = {"bars": int(len(df)),
            "first": int(df["time"].iloc[0]) if len(df) else 0,
            "last": int(df["time"].iloc[-1]) if len(df) else 0,
            "updated": time.time()}
    tmp = _archive_path(symbol) + ".meta.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, _archive_path(symbol) + ".meta")
    except Exception:
        pass
    return meta


def _load_archive_df(symbol):
    """Full M1 archive as a DataFrame - background jobs only, never the
    request path (a multi-million-row read costs seconds)."""
    p = _archive_path(symbol)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        if "time" not in df.columns or len(df) == 0:
            return None
        return df.sort_values("time").reset_index(drop=True)
    except Exception:
        return None


def _load_archive_tail(symbol, n):
    """Newest n archive bars as JSON dicts (for deep scroll-back past the
    working set). Reads the big file - call sparingly, off the hot path."""
    df = _load_archive_df(symbol)
    if df is None:
        return []
    tail = df.tail(n)
    out = []
    for r in tail.itertuples(index=False):
        try:
            v = r.volume
        except AttributeError:
            v = 0
        out.append({"time": int(r.time), "open": float(r.open),
                    "high": float(r.high), "low": float(r.low),
                    "close": float(r.close), "volume": int(v or 0)})
    return out


def queue_deep_sync(symbol, reason="manual"):
    """Enqueue a max-depth sync for `symbol` (deduped). Returns True when
    the symbol was actually queued."""
    sym = (symbol or "").strip()
    if not sym:
        return False
    with _DEEP_SYNC_LOCK:
        if sym in _DEEP_SYNC_ACTIVE:
            return False
        _DEEP_SYNC_ACTIVE.add(sym)
        _DEEP_SYNC_QUEUE.append(sym)
    print("[chart] deep sync queued for %s (%s)" % (sym, reason), flush=True)
    return True


def _wanted_brick_sets(symbol):
    """(box, base_tf) pairs worth caching for `symbol`: the default, every
    1m-base box with a live chain, and everything the client registered."""
    wanted = {(2.5, "1m")}
    try:
        with _RENKO_IND_LOCK:
            for (s, box, btf) in list(_RENKO_IND_CHAINS.keys()):
                if s == symbol and btf == "1m":
                    wanted.add((round(float(box), 3), btf))
    except Exception:
        pass
    for (s, box, btf) in list(_BRICK_WANT):
        if s == symbol and btf == "1m":
            wanted.add((round(float(box), 3), btf))
    return wanted


def _brick_cache_paths(symbol, box, base_tf):
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in symbol)
    d = os.path.join(DATA_DIR, "renko")
    # feed-namespaced - bricks built from OANDA M1 differ from the broker's.
    base = os.path.join(d, "%s_%s_%s%s" % (safe, round(float(box), 3), base_tf,
                                          _FEED_TAG))
    return base + ".parquet", base + ".meta"


def _build_brick_cache(symbol, box, base_tf, df=None):
    """Build the full-depth brick series for (symbol, box, 1m-base) from the
    M1 archive and persist it. Streams rows - never materialises millions
    of dicts. Returns True on success."""
    if base_tf != "1m":
        return False
    if df is None:
        df = _load_archive_df(symbol)
    if df is None or len(df) == 0:
        return False
    op_id = "bricks:%s:%s" % (symbol, round(float(box), 3))
    try:
        ops.start(op_id, "Renko bricks %s box %s" % (symbol, box))
        chain = RenkoChain(round(float(box), 3), 60)
        cols = df[["time", "open", "high", "low", "close"]]
        now = time.time()
        it = cols.itertuples(index=False)
        r0 = next(it, None)
        if r0 is None:
            ops.fail(op_id, "empty archive")
            return False
        chain.anchor = float(r0.open)
        chain._last_ts = int(r0.time)
        b = {}  # reused scratch dict - _process_bar only reads it
        n = 1
        for r in it:
            bt = int(r.time)
            if bt + 60 > now:
                break  # forming bucket: the live chain owns it
            b["time"] = bt
            b["open"] = float(r.open)
            b["high"] = float(r.high)
            b["low"] = float(r.low)
            b["close"] = float(r.close)
            chain._process_bar(b)
            chain.last_bar_t = bt
            n += 1
            if n % 1000000 == 0:
                ops.touch(op_id, "%d bars bricked..." % n)
        bricks = chain.bricks
        if not bricks:
            ops.fail(op_id, "no bricks built")
            return False
        p, mp = _brick_cache_paths(symbol, box, base_tf)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        bdf = pd.DataFrame(bricks, columns=["time", "open", "high", "low",
                                            "close", "dir"])
        bdf.to_parquet(p, index=False)
        am = _archive_meta(symbol) or {}
        meta = {"box": round(float(box), 3), "base_tf": base_tf,
                "anchor": chain.anchor, "last_bar_t": int(chain.last_bar_t),
                "_last_ts": int(chain._last_ts),
                "archive_first": am.get("first"), "archive_last": am.get("last"),
                "archive_bars": am.get("bars"), "bricks": len(bricks),
                "built_at": time.time()}
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, mp)
        ops.done(op_id, "%d bricks cached" % len(bricks))
        print("[chart] brick cache %s box %s: %d bricks" % (symbol, box, len(bricks)),
              flush=True)
        return True
    except Exception as e:
        try:
            ops.fail(op_id, str(e))
        except Exception:
            pass
        print("[chart] brick cache build failed %s %s: %s" % (symbol, box, e),
              flush=True)
        return False


def _seed_chain_from_brick_cache(chain, symbol, box, base_tf):
    """Instant chain seeding from the disk brick cache. The cache is valid
    only when it was built from the same archive start - a backward
    extension re-anchors the brick grid, so a stale cache must NOT seed."""
    if base_tf != "1m":
        return False
    try:
        p, mp = _brick_cache_paths(symbol, box, base_tf)
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
        am = _archive_meta(symbol)
        if not am:
            return False
        if meta.get("archive_first") != am.get("first"):
            return False  # archive grew older - grid re-anchored, rebuild
        if (meta.get("archive_bars") or 0) > (am.get("bars") or 0):
            return False
        bdf = pd.read_parquet(p)
        if len(bdf) == 0:
            return False
        bricks = []
        for r in bdf.itertuples(index=False):
            bricks.append({"time": int(r.time), "open": float(r.open),
                           "high": float(r.high), "low": float(r.low),
                           "close": float(r.close), "forming": False,
                           "dir": int(r.dir)})
        chain.bricks = bricks
        chain.anchor = float(meta["anchor"])
        chain.last_bar_t = int(meta["last_bar_t"])
        chain._last_ts = int(meta["_last_ts"])
        return True
    except Exception:
        return False


def _run_deep_sync(symbol):
    """One max-depth sync: forward tail-gap fill, then backward extension to
    the broker's oldest bar, then Renko brick caches. Incremental - a repeat
    run only fetches what's missing."""
    op_id = "deephist:" + symbol
    ops.start(op_id, "Deep history %s" % symbol)
    if not feed.live:
        ops.done(op_id, "demo mode - bundled history is already complete")
        return
    now = int(time.time())
    df = _load_archive_df(symbol)
    if df is None:
        # First run: seed the archive from the working file (no data loss -
        # the 20k bars already on disk become the archive's newest 20k).
        ops.stage(op_id, "Seeding from local cache",
                  "reading the bars already on disk")
        bars = _load_disk(symbol, "1m")
        if bars:
            df = pd.DataFrame(
                [{"time": b["time"], "open": b["open"], "high": b["high"],
                  "low": b["low"], "close": b["close"],
                  "volume": b.get("volume", 0)} for b in bars])
            df = df.sort_values("time").reset_index(drop=True)
    if df is None or len(df) == 0:
        # Nothing at all: pull the newest chunk from MT5 as the seed.
        ops.stage(op_id, "First pull from MT5", "fetching the newest %d bars"
                  % _DEEP_CHUNK_BARS)
        seed = feed.get_rates_range(symbol, "1m", now - _DEEP_CHUNK_BARS * 60,
                                    now)
        if seed is None or len(seed) == 0:
            ops.fail(op_id, "%s returned no data for %s" % (_FEED_NAME, symbol))
            return
        df = seed.sort_values("time").reset_index(drop=True)
    # --- forward: archive tail -> now (the usual minutes-old gap) ----
    last_t = int(df["time"].iloc[-1])
    if last_t < now - 120:
        ops.stage(op_id, "Catching up to live",
                  "filling %s -> now" % time.strftime("%Y-%m-%d %H:%M",
                                                      time.gmtime(last_t)))
        try:
            tail = feed.get_rates_range(symbol, "1m", last_t - 300, now)
            if tail is not None and len(tail):
                df = (pd.concat([df, tail]).drop_duplicates("time")
                      .sort_values("time").reset_index(drop=True))
        except Exception as e:
            ops.touch(op_id, "tail fill failed (%s) - continuing with the "
                             "backward walk" % e)
    # --- backward: walk to the broker's oldest bar ----
    total = len(df)
    oldest = int(df["time"].iloc[0])
    first_before = oldest
    short_streak = 0
    while total < _DEEP_MAX_BARS and short_streak < 2:
        cs = oldest - _DEEP_CHUNK_BARS * 60
        ce = oldest - 60
        ops.stage(op_id, "Fetching history",
                  "%s bars - oldest %s" %
                  ("{:,}".format(total),
                   time.strftime("%Y-%m-%d", time.gmtime(oldest))))
        try:
            chunk = feed.get_rates_range(symbol, "1m", cs, ce)
        except Exception as e:
            ops.touch(op_id, "%s pull failed (%s) - retrying once" % (_FEED_NAME, e))
            time.sleep(2)
            try:
                chunk = feed.get_rates_range(symbol, "1m", cs, ce)
            except Exception:
                chunk = []
        n = len(chunk) if chunk is not None else 0
        if n == 0:
            # A 69-day window with zero bars: almost certainly past the
            # broker's start - but allow one more probe before giving up, in
            # case the terminal is mid-sync on that stretch.
            short_streak += 1
            oldest = cs
            continue
        df = (pd.concat([chunk, df]).drop_duplicates("time")
              .sort_values("time").reset_index(drop=True))
        total = len(df)
        oldest = int(df["time"].iloc[0])
        short_streak = short_streak + 1 if n < _DEEP_CHUNK_BARS * 0.5 else 0
    try:
        df.to_parquet(_archive_path(symbol), index=False)
    except Exception as e:
        ops.fail(op_id, "could not write the archive: %s" % e)
        return
    meta = _write_archive_meta(symbol, df)
    grew_older = meta["first"] < first_before
    ops.stage(op_id, "Building Renko bricks",
              "%s bars archived - bricking from the same data" %
              "{:,}".format(meta["bars"]))
    for (box, btf) in sorted(_wanted_brick_sets(symbol)):
        _build_brick_cache(symbol, box, btf, df)
    if grew_older:
        # The brick grid re-anchored at the older first bar - live chains
        # must re-seed from the fresh cache instead of appending to the old
        # grid on next access.
        try:
            with _RENKO_IND_LOCK:
                for k in [k for k in _RENKO_IND_CHAINS if k[0] == symbol]:
                    del _RENKO_IND_CHAINS[k]
        except Exception:
            pass
    ops.done(op_id, "%s bars (%s -> %s)" %
             ("{:,}".format(meta["bars"]),
              time.strftime("%Y-%m-%d", time.gmtime(meta["first"])),
              time.strftime("%Y-%m-%d", time.gmtime(meta["last"]))),
             result={"bars": meta["bars"], "first": meta["first"],
                     "last": meta["last"]})
    print("[chart] deep sync %s done: %s bars" % (symbol, "{:,}".format(meta["bars"])),
          flush=True)
    del df


def _deep_sync_worker():
    """Single background worker: symbols sync one at a time so MT5 is never
    hammered with parallel deep pulls."""
    while True:
        sym = None
        with _DEEP_SYNC_LOCK:
            if _DEEP_SYNC_QUEUE:
                sym = _DEEP_SYNC_QUEUE.pop(0)
        if sym is None:
            time.sleep(2)
            continue
        try:
            _run_deep_sync(sym)
        except Exception as e:
            try:
                ops.fail("deephist:" + sym, str(e))
            except Exception:
                pass
            print("[chart] deep sync %s crashed: %s" % (sym, e), flush=True)
        finally:
            with _DEEP_SYNC_LOCK:
                _DEEP_SYNC_ACTIVE.discard(sym)


def _backfill_full(symbol, tf, target):
    if tf in _TF_NATIVE:
        df = feed.get_rates_deep(symbol, tf, target)
    else:
        mult = _TF_M1_MULT.get(tf, 1)
        df = feed.get_m1_deep(symbol, target * mult + mult)
        df = Feed.resample(df, mult).tail(target)
    return _bars_to_json(df)


# standalone history database (external data folder, tick-priority).
# Seeds deep history from the user's 17-year XAUUSD database when the local
# disk cache is empty. The DB is NEVER written by the app - the user manages
# the file in a separate folder; the app only reads. Tick-derived candles
# take priority over M1; both merge into one seamless series.
def _history_db_seed(symbol, tf, target):
    """Newest `target` bars from the external history DB, or None.

    Only applies to gold symbols (the DB is XAUUSD-only). Returns bars
    oldest->newest without the internal 'source' marker.

    ONLY for M1-derived timeframes (1m/3m/5m). For native MT5
    timeframes (15m+), the DB's UTC-midnight bucketing misaligns with MT5's
    server-midnight 4H/D bars, creating "ghost" duplicate candles. Native
    TFs load from MT5 directly.
    """
    try:
        # DB aggregation is only safe for M1 multiples - native MT5 TFs
        # have server-aligned boundaries that UTC bucketing can't match.
        if tf not in ("1m", "1", "3m", "3", "5m", "5"):
            return None
        sym = (symbol or "").upper()
        if "XAUUSD" not in sym and not sym.startswith("GOLD"):
            return None
        from history_db import get_db
        db = get_db()
        if not db.available():
            return None
        tf_sec = _TF_SEC.get(tf, 60)
        now = int(time.time())
        # Ask for 2x the span to survive weekend gaps, then trim to target.
        start = now - target * tf_sec * 2
        bars = db.get_candles(tf, start, now, limit=target)
        for b in bars:
            b.pop("source", None)
        return bars or None
    except Exception:
        return None


def _fetch_tail(symbol, tf, n):
    """Newest ~n bars (cheap single MT5 pull, for gap backfill).

    bounded - a hung tail pull behaves like a failed one (the
    callers catch it), never wedges the request.
    """
    def _do():
        if tf in _TF_NATIVE:
            df = feed.get_rates(symbol, tf, n)
        else:
            mult = _TF_M1_MULT.get(tf, 1)
            df = Feed.resample(feed.get_m1(symbol, n * mult + mult), mult)
        return _bars_to_json(df)

    return _mt5_bounded(_do, 30, "tail pull")


# Cap on vault-healed bars per history call (bounds the SQLite read; the most
# recent gaps are healed first, older ones on the next call).
_MAX_HEAL_BARS = 5000

# (symbol, tf) -> [f_time, f_close, first_seen_ts]: remembers the last MT5
# forming bar so a *frozen* one (same time+close while fresh quotes keep
# arriving) can be detected and substituted from the tick vault.
_LIVEBAR_SEEN = {}
# How long MT5's forming bar may sit unchanged while quotes flow before the
# vault takes over drawing the live candle (seconds).
_LIVEBAR_FROZEN_S = 60


def _heal_live_bar(symbol, tf, live_bar, q):
    """Substitute a frozen/missing MT5 forming bar with the tick vault's.

    Normal case: MT5's forming bar is fresh (it sees every tick, we only poll
    1/sec) - leave it alone, it is more accurate. But when MT5's bar stops
    incorporating new quotes for over a minute while our vault keeps
    recording them (the frozen-candle reports), the chart would show a dead
    candle under a live price line. Then the vault - real recorded prices -
    draws the live candle instead, marked {"local": True, "healed": True}.
    The client adopts it exactly like MT5's (zero client changes); when MT5
    recovers, its bar takes over again automatically.

    Zombie case: MT5 can serve a PAST slot as "forming" while its close keeps
    jittering with live ticks (rate history froze, tick stream alive). The
    (time, close) comparison below never trips then - every close jitter
    resets the 60s timer - so a stale slot is detected on the slot alone and
    healed at once.
    """
    try:
        if not feed.live or not q or not live_bar:
            return
        tf_sec = _TF_SEC.get(tf)
        if not tf_sec:
            return  # 1s/15s tick buckets, Renko and D/W/M keep their own paths
        now = int(time.time())
        if int(q.get("time", 0)) < now - 15:
            return  # quotes themselves are stale - nothing to heal with
        cur_slot = (now // tf_sec) * tf_sec
        f = live_bar.get("forming") or {}
        key = (symbol, tf)
        # -- zombie forming bar: a past slot is never a valid forming bar.
        # MT5's clock (the quote time) defines "now", so server/MT5 clock skew
        # can't false-trip this. One slot of tolerance: MT5 rolls the slot
        # with sub-second lag right on the boundary.
        qt = int(q.get("time", 0)) or now
        slot_now = (qt // tf_sec) * tf_sec
        f_time = int(f.get("time", 0)) if f else 0
        if f and f_time < slot_now - tf_sec:
            zbars = vault.build_bars(symbol, slot_now - 2 * tf_sec, now, tf_sec)
            if zbars:
                zforming = zbars[-1]
                if zforming["time"] >= slot_now - tf_sec:
                    live_bar["forming"] = zforming
                    zprev = (zbars[-2] if len(zbars) > 1 and
                             zbars[-2]["time"] < zforming["time"] else None)
                    if zprev:
                        live_bar["prev_closed"] = zprev
                    live_bar["healed"] = True
                    _LIVEBAR_SEEN[key] = [
                        (int(zforming["time"]),
                         round(float(zforming["close"]), 3)), now]
            return
        cur = (int(f.get("time", 0)), round(float(f.get("close", 0)), 3)) if f else None
        seen = _LIVEBAR_SEEN.get(key)
        if seen is None or seen[0] != cur:
            _LIVEBAR_SEEN[key] = [cur, now]
            if cur is not None:
                return  # MT5's bar changed -> healthy, its bar is authoritative
            # else: MT5 returned no forming bar at all - fall through and
            # heal from the vault immediately (no 60s wait for a void).
        elif now - seen[1] < _LIVEBAR_FROZEN_S:
            return
        newest = vault.newest(symbol)
        if not newest or newest < cur_slot - 3 * tf_sec:
            return  # vault has nothing fresher either
        bars = vault.build_bars(symbol, cur_slot - 2 * tf_sec, now, tf_sec)
        if not bars:
            return
        forming = bars[-1]
        if forming["time"] < cur_slot - tf_sec:
            return  # vault couldn't build the current slot either
        prev = (bars[-2] if len(bars) > 1 and bars[-2]["time"] < forming["time"]
                else None)
        live_bar["forming"] = forming
        if prev:
            live_bar["prev_closed"] = prev
        live_bar["healed"] = True
    except Exception:
        pass


def _heal_gaps(symbol, tf, bars, target):
    """Fill bar slots MT5 didn't serve from the local tick vault.

    MT5's rate history can freeze or skip chunks while its tick stream keeps
    flowing (the frozen/missing-candle reports). The vault holds our own
    1-second recording of every quote the server saw, so any slot with tick
    coverage becomes a real bar marked {"local": True} instead of blank chart
    space. Slots with no tick coverage (market closed, server was down) are
    left alone - never fabricated. When MT5 later serves the real bar, the
    normal tail merge overwrites the local one (MT5 wins ties).
    """
    tf_sec = _TF_SEC.get(tf)
    if not tf_sec or not bars or not feed.live:
        return bars
    try:
        have = {b["time"] for b in bars}
        start, end = bars[0]["time"], bars[-1]["time"]
        ranges = []
        rs = None
        t = start
        # Interior gaps: slots between first and last bar that MT5 skipped.
        while t <= end:
            if t not in have:
                rs = (rs[0], t) if rs else (t, t)
            elif rs:
                ranges.append(rs)
                rs = None
            t += tf_sec
        if rs:
            ranges.append(rs)
        # Tail: MT5 history behind the live market (closed slots only - the
        # forming slot belongs to /api/quote's live_bar).
        now = int(time.time())
        cur_slot = (now // tf_sec) * tf_sec
        if end < cur_slot - tf_sec:
            ranges.append((end + tf_sec, cur_slot - tf_sec))
        if not ranges:
            return bars
        # Most-recent gaps first, bounded.
        ranges.sort(reverse=True)
        healed = []
        for a, b in ranges:
            need = _MAX_HEAL_BARS - len(healed)
            if need <= 0:
                break
            span_slots = (b - a) // tf_sec + 1
            if span_slots > need:
                a = b - (need - 1) * tf_sec
            healed.extend(vault.build_bars(symbol, a, b + tf_sec - 1, tf_sec))
            if len(healed) >= _MAX_HEAL_BARS:
                break
        if not healed:
            return bars
        merged = {b["time"]: b for b in bars}
        for b in healed:
            if b["time"] not in merged:   # MT5 data always wins ties
                merged[b["time"]] = b
        return [merged[k] for k in sorted(merged)][-target:]
    except Exception:
        return bars


# tick-history gap fill bounds.
_MAX_TICKFILL_SLOTS = 1440  # 1m-equivalent slots per heal (24h of 1m)
_TICKFILL_TIMEOUT = 30      # seconds per copy_ticks_range (mirrors gap fill)


def _drop_glitch_ticks(ticks):
    """drop single-tick broker feed glitches before aggregation.

    A bad tick (feed glitch) prints far from the market and the stream snaps
    straight back - that is exactly how the 02:30 1m cliff got
    baked into a tick-rebuilt bar while MT5's own M1 chart stayed clean.
    A tick is a glitch when it jumps more than T from the previous tick AND
    the stream is back near the pre-jump price within the next two ticks.
    Real moves persist across ticks, so they are never flagged. T is $8 for
    XAUUSD (0.2% of price for other symbols). Edge ticks are always kept: a
    glitch at the very start/end of the pull cannot be verified.
    Returns a sorted list of (ms, bid) pairs.
    """
    vals = []
    for t in ticks:
        try:
            vals.append((int(t[0]), float(t[1])))
        except (TypeError, ValueError, IndexError):
            continue
    vals.sort(key=lambda v: v[0])
    n = len(vals)
    if n < 4:
        return vals
    ref = abs(vals[0][1]) or 1.0
    T = max(8.0, 0.002 * ref)
    px = [v[1] for v in vals]
    bad = set()
    for i in range(1, n - 1):
        if i in bad or abs(px[i] - px[i - 1]) <= T:
            continue
        # one-tick spike: the very next tick is already back
        if abs(px[i + 1] - px[i - 1]) <= T * 0.5:
            bad.add(i)
        # two-tick spike: both ticks far, stream back on the following one
        elif i + 2 < n and abs(px[i + 1] - px[i - 1]) > T \
                and abs(px[i + 2] - px[i - 1]) <= T * 0.5:
            bad.add(i)
            bad.add(i + 1)
    if not bad:
        return vals
    print("[chart] tick-glitch filter: dropped %d/%d spike tick(s)"
          % (len(bad), n))
    return [v for i, v in enumerate(vals) if i not in bad]


def _ticks_to_bars(ticks, tf_sec):
    """Aggregate [(ms, bid, ask)] ticks into bid-based OHLC bars.

    MT5 history bars are bid-based (same as the local vault's recordings),
    so bid keeps tick-built bars consistent with surrounding MT5 bars.
    Returns [{time, open, high, low, close, volume, tickbuilt: True}] -
    one bar per tf slot with at least one tick; empty slots are omitted,
    never fabricated. volume = tick count in the slot, which is exactly
    what MT5 tick volume means.

    single-tick feed glitches are dropped first (_drop_glitch_ticks)
    so one bad print cannot bake a false cliff into a rebuilt bar.
    """
    ticks = _drop_glitch_ticks(ticks)
    groups = {}
    for ms, bid in ticks:
        slot = (ms // 1000 // tf_sec) * tf_sec
        g = groups.get(slot)
        if g is None:
            groups[slot] = [bid, bid, bid, bid, 1]
        else:
            if bid > g[1]:
                g[1] = bid
            if bid < g[2]:
                g[2] = bid
            g[3] = bid
            g[4] += 1
    bars = [{"time": s, "open": round(g[0], 3), "high": round(g[1], 3),
             "low": round(g[2], 3), "close": round(g[3], 3),
             "volume": g[4], "tickbuilt": True}
            for s, g in groups.items()]
    bars.sort(key=lambda b: b["time"])
    return bars


def _fill_tick_history_gaps(symbol, tf, bars, diag):
    """Rebuild bars MT5's rate history won't serve from its tick history.

    copy_rates_range can return nothing for a range the broker still holds
    tick-by-tick (the 13:20-15:30 IST hole). Real ticks aggregate
    into exact candles, so pull copy_ticks_range for the still-missing
    ranges and build bid-based bars. Bounded: most-recent gaps first, at
    most _MAX_TICKFILL_SLOTS slots, _TICKFILL_TIMEOUT watchdog per range.
    Ranges with no ticks (market closed, broker pruned) stay missing and
    are reported honestly by heal_data - never fabricated. MT5 rate bars
    always win ties.
    """
    tf_sec = _TF_SEC.get(tf)
    if not tf_sec or not bars or not feed.live:
        return bars
    gaps = _scan_gaps(bars, tf_sec,
                      skip_weekends=not _trades_weekends(symbol))
    if not gaps:
        return bars
    gaps.sort(key=lambda g: g[0], reverse=True)  # most-recent first
    have = {b["time"] for b in bars}
    filled = []
    for a, b in gaps:
        need = _MAX_TICKFILL_SLOTS - len(filled)
        if need <= 0:
            break
        if (b - a) // tf_sec + 1 > need:
            a = b - (need - 1) * tf_sec
        try:
            ticks = _mt5_bounded(
                lambda a=a, b=b: feed.get_ticks_range(symbol, a, b + tf_sec),
                _TICKFILL_TIMEOUT, "tick history")
        except Exception:
            ticks = None
        if not ticks:
            continue
        for tb in _ticks_to_bars(ticks, tf_sec):
            if tb["time"] not in have and a <= tb["time"] <= b:
                have.add(tb["time"])
                filled.append(tb)
        if len(filled) >= _MAX_TICKFILL_SLOTS:
            break
    if not filled:
        return bars
    merged = {b["time"]: b for b in bars}
    for b in filled:
        if b["time"] not in merged:  # MT5 rate bars always win ties
            merged[b["time"]] = b
    try:
        diag["tick_bars_added"] = diag.get("tick_bars_added", 0) + len(filled)
        print("[chart] tick-history fill: %d bar(s) for %s %s "
              "(ranges MT5 rate history would not serve)"
              % (len(filled), symbol, tf))
    except Exception:
        pass
    return [merged[k] for k in sorted(merged)]


def _has_interior_gaps(bars, tf_sec, symbol=None):
    """cheap check - does this bar list have interior holes?

    only reports gaps in the last 7 days. Older holes are almost
    always weekends/holidays (no XAUUSD bars Sat/Sun) - fetching those from
    MT5 on every chart load exhausted the browser's connection pool and
    wedged the whole DATA panel.

    crypto trades 24/7 - weekend holes ARE gaps for it, so the
    Sat/Sun skip below is disabled when _trades_weekends(symbol).
    """
    if not bars or not tf_sec or len(bars) < 3:
        return False
    try:
        cutoff = time.time() - 7 * 86400
        # Only consider recent bars.
        recent = [b for b in bars if b["time"] >= cutoff]
        if len(recent) < 3:
            return False
        have = {b["time"] for b in recent}
        t, end = recent[0]["time"], recent[-1]["time"]
        span = (end - t) // tf_sec
        if span <= 0:
            return False
        step = max(1, span // 200)
        s = t + tf_sec
        while s < end:
            if s not in have:
                # skip Sat/Sun - no XAUUSD bars on weekends.
                # ..unless the symbol trades weekends (crypto).
                import datetime as _dt
                wd = _dt.datetime.fromtimestamp(s, tz=_dt.timezone.utc).weekday()
                if wd < 5 or _trades_weekends(symbol):
                    return True
            s += step * tf_sec
        return False
    except Exception:
        return False


# remember recent gap-fill attempts so one slow MT5 doesn't get
# re-queried on every chart refresh.
_GAPFILL_LAST = {}
_GAPFILL_TTL = 3600  # seconds

# a fill that found gaps but fetched NOTHING (MT5 still syncing
# after PC sleep/wake, transient error) must NOT consume the 1h TTL -
# otherwise the hole survives an hour in the cache. Failed attempts only
# suppress retries for _GAPFILL_RETRY seconds.
_GAPFILL_FAIL = {}
_GAPFILL_RETRY = 300  # seconds (5 min)

# throttle the interior-gap scan (runs on every poll otherwise).
_GAPCHECK_LAST = {}
_GAPCHECK_TTL = 300  # seconds (5 min)

# data-integrity registry - (symbol, tf) -> last-used unix ts.
# Updated by _deep_history so the background integrity loop knows which
# series are actually being watched and heals those.
_HEAL_ACTIVE = {}

# per-series diagnostics from the last _deep_history call -
# (symbol, tf) -> {"tail_ok": bool, "tail_err": str|None,
#                  "ranges_found": int, "bars_added": int, "at": ts}.
# Powers the Fix-data progress/completion dialog with honest reporting
# (e.g. "MT5 returned no data - terminal may still be syncing" instead of
# a misleading "data is complete").
_DEEP_DIAG = {}
_HEAL_ACTIVE_TTL = 7200  # forget a series after 2h of disuse
# background integrity loop cadence.
_HEAL_LOOP_SECS = 600  # 10 minutes
_HEAL_LOOP_FIRST_SECS = 60  # first run 60s after server start (startup heal)


def _has_interior_gaps_throttled(symbol, tf, bars, tf_sec):
    """cached wrapper - the full scan runs at most once per 5 min
    per symbol/tf. Returns the last known result otherwise."""
    now = time.time()
    gk = (symbol, tf)
    last = _GAPCHECK_LAST.get(gk)
    if last and now - last[0] < _GAPCHECK_TTL:
        return last[1]
    result = _has_interior_gaps(bars, tf_sec, symbol)
    _GAPCHECK_LAST[gk] = (now, result)
    return result


def _suspect_slots(bars, tf_sec):
    """slots whose bar looks like a baked-in price glitch.

    Flags a closed bar whose range exceeds BOTH an absolute floor
    (15*sqrt(tf_minutes) dollars - a feed glitch prints $-tens on any TF)
    and 6x the median range of its +-10 neighbours. Real volatility moves
    the neighbours too, so the median comparison keeps news spikes safe;
    a lone cliff (like the 02:30 1m bar) stands out. Returns a
    set of bar times. The forming bar is never suspect.
    """
    if not bars or len(bars) < 12:
        return set()
    now_slot = int(time.time() // tf_sec) * tf_sec
    ranges = []
    for b in bars:
        try:
            ranges.append(max(0.0, float(b["high"]) - float(b["low"])))
        except (TypeError, ValueError):
            ranges.append(0.0)
    import statistics as _st
    floor = 15.0 * (tf_sec / 60) ** 0.5
    out = set()
    n = len(bars)
    for i, b in enumerate(bars):
        t = b["time"]
        if t >= now_slot or ranges[i] <= floor:
            continue
        lo, hi = max(0, i - 10), min(n, i + 11)
        nb = [ranges[j] for j in range(lo, hi) if j != i]
        if len(nb) < 5:
            continue
        med = _st.median(nb)
        if med > 0 and ranges[i] > 6 * med:
            out.add(t)
    return out


def _fill_mt5_gaps(symbol, tf, bars):
    """surgically fill interior gaps from MT5.

    A chunked deep pull can bake holes into the disk cache when the terminal
    is still syncing history (the 05:29->06:29 IST gap on ). The
    tail merge only reaches ~120 bars back and the tick vault only covers
    periods the server was actually running, so such holes were permanent.
    This finds every interior gap and fetches exactly that time range via
    copy_rates_range, then merges. MT5 data always wins ties.

    throttled to once an hour per symbol/tf, and weekend slots are
    never fetched (no XAUUSD bars Sat/Sun - fetching them on every chart
    load exhausted the browser connection pool).
    """
    tf_sec = _TF_SEC.get(tf)
    if not tf_sec or not bars or not feed.live:
        return bars
    now = time.time()
    gk = (symbol, tf)
    if now - _GAPFILL_LAST.get(gk, 0) < _GAPFILL_TTL:
        return bars
    # a failed fill only suppresses retries for 5 min (see header).
    if now - _GAPFILL_FAIL.get(gk, 0) < _GAPFILL_RETRY:
        return bars
    try:
        # report findings for the Fix-data dialog. _deep_history
        # stores its diag dict here before calling us, so this mutates the
        # same object the heal endpoint later reads.
        dd = _DEEP_DIAG.setdefault((symbol, tf), {})
        dd["ranges_found"] = 0
        dd["bars_added"] = 0
        dd["bars_repaired"] = 0
        have = {b["time"] for b in bars}
        # re-verify slots holding OUR reconstructions (tick-built
        # bars from, vault bars) - MT5's authoritative rates win once
        # the terminal has synced them. Also re-verify bars that look like
        # baked-in price glitches (the 02:30 1m cliff): a bad tick
        # in the tick history can print a false $-tens spike that MT5's own
        # M1 chart never shows. Both sets are re-fetched below and replaced.
        by_time = {b["time"]: b for b in bars}
        recon = {t for t, b in by_time.items()
                 if b.get("tickbuilt") or b.get("local")}
        forming = int(now // tf_sec) * tf_sec
        suspect = {t for t in _suspect_slots(bars, tf_sec) if t < forming}
        reverify = recon | suspect
        ranges = []
        t, end = bars[0]["time"], bars[-1]["time"]
        rs = None
        while t <= end:
            if t not in have or t in reverify:
                # never treat a weekend slot as a gap (no XAUUSD
                # bars Sat/Sun - fetching them wedged the DATA panel).
                import datetime as _dt
                if _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).weekday() < 5:
                    rs = (rs[0], t) if rs else (t, t)
                elif rs:
                    ranges.append(rs)
                    rs = None
            elif rs:
                ranges.append(rs)
                rs = None
            t += tf_sec
        if rs:
            ranges.append(rs)
        dd["ranges_found"] = len(ranges)
        if not ranges:
            # clean scan - nothing to fill for the next hour.
            _GAPFILL_LAST[gk] = now
            return bars
        # Most recent first; cap total fetched bars to stay cheap.
        ranges.sort(reverse=True)
        filled = []
        budget = 5000
        for a, b in ranges:
            span = (b - a) // tf_sec + 1
            if span > budget:
                a = b - (budget - 1) * tf_sec
                span = budget
            df = feed.get_rates_range(symbol, tf, a, b)
            if df is not None and len(df):
                filled.extend(_bars_to_json(df))
            budget -= span
            if budget <= 0:
                break
        if not filled:
            # gaps found but MT5 gave nothing (still syncing after
            # sleep/wake, transient error) - retry in 5 min, NOT 1 hour.
            # log the exact ranges so the server log shows WHAT
            # is missing instead of a silent hole the dialog would call
            # "data is complete".
            _GAPFILL_FAIL[gk] = now
            try:
                _rng_txt = ", ".join(
                    _fmt_range_ist(a, b) for a, b in ranges[:3])
                if len(ranges) > 3:
                    _rng_txt += " +%d more" % (len(ranges) - 3)
            except Exception:
                _rng_txt = "%d range(s)" % len(ranges)
            print("[chart] gap-fill %s %s: %d range(s) still missing, "
                  "%s returned no data: %s"
                  % (symbol, tf, len(ranges), _FEED_NAME, _rng_txt))
            return bars
        merged = {b["time"]: b for b in bars}
        n_new = 0
        n_rep = 0
        for b in filled:
            t = b["time"]
            if t not in merged:
                merged[t] = b
                n_new += 1
            elif t in reverify and t < forming:
                # our reconstruction / glitch-suspect bar - MT5's
                # authoritative rate supersedes it. Never touches the
                # forming bar (live quotes own it).
                merged[t] = b
                n_rep += 1
        dd["bars_added"] = n_new
        dd["bars_repaired"] = n_rep
        if n_new:
            print("[chart] gap-fill %s %s: +%d bars from MT5" %
                  (symbol, tf, n_new))
        if n_rep:
            print("[chart] gap-fill %s %s: %d bar(s) repaired from MT5 "
                  "(reconstruction/glitch superseded)" % (symbol, tf, n_rep))
        if n_new or n_rep:
            _GAPFILL_LAST[gk] = now  # successful fill - 1h quiet
        else:
            _GAPFILL_FAIL[gk] = now  # fetched but added nothing
        target = _DEEP_BARS.get(tf, 600)
        return [merged[k] for k in sorted(merged)][-target:]
    except Exception as e:
        print("[chart] gap-fill failed: %s" % e)
        _GAPFILL_FAIL[gk] = now  # transient - retry in 5 min
        return bars


# bound for the MT5 gap-fill inside the request-critical
# _deep_history path. On a half-dead terminal (PC sleep/wake while the
# terminal is still syncing history) copy_rates_range can BLOCK
# indefinitely instead of raising.called it unguarded after a
# failed tail pull, so every _deep_history (startup warm thread + each
# /api/candles) hung forever: blank charts and a stuck "Connecting to
# server..." overlay, while endpoints that never touch history
# (watchlist/alerts/status) kept working. The fill now runs in a daemon
# worker; the caller proceeds after _GAPFILL_TIMEOUT_S with whatever bars
# it has, and retries back off for 5 min .
_GAPFILL_TIMEOUT_S = 30
_GAPFILL_RUNNING = set()
_GAPFILL_RUNNING_LOCK = threading.Lock()


def _fill_mt5_gaps_bounded(symbol, tf, bars, diag):
    """_fill_mt5_gaps that can never wedge the caller.

    Returns the (possibly gap-filled) bars. On timeout the unfilled bars
    are returned immediately; the stuck worker stays a daemon and dies
    with the process."""
    gk = (symbol, tf)
    with _GAPFILL_RUNNING_LOCK:
        if gk in _GAPFILL_RUNNING:
            return bars  # another thread is already filling; don't pile on
        _GAPFILL_RUNNING.add(gk)
    box = {}

    def _work():
        try:
            box["bars"] = _fill_mt5_gaps(symbol, tf, bars)
        except Exception as e:  # _fill_mt5_gaps guards itself; belt & braces
            box["error"] = e
        finally:
            with _GAPFILL_RUNNING_LOCK:
                _GAPFILL_RUNNING.discard(gk)

    w = threading.Thread(target=_work, daemon=True)
    w.start()
    w.join(_GAPFILL_TIMEOUT_S)
    if w.is_alive():
        _GAPFILL_FAIL[gk] = time.time()  # back off retries 5 min
        with _GAPFILL_RUNNING_LOCK:
            _GAPFILL_RUNNING.discard(gk)
        note = ("gap fill timed out after %ds (MT5 not responding)"
                % _GAPFILL_TIMEOUT_S)
        if diag is not None:
            prev = diag.get("tail_err") or ""
            diag["tail_err"] = (prev + "; " + note).strip("; ")
            diag["tail_ok"] = False
        print("[chart] gap-fill %s %s: %s" % (symbol, tf, note))
        return bars
    if "error" in box:
        return bars
    return box.get("bars", bars)


def _mt5_bounded(fn, timeout, what):
    """Run an MT5 history call in a daemon worker; give up after `timeout`.

    extends thegap-fill guard to every MT5 history call on
    the request path (tick pulls, tail pulls, deep backfills). A terminal
    that stops answering must degrade to an honest error, never wedge the
    UI. Raises RuntimeError on timeout; the call's own exceptions propagate
    unchanged.
    """
    out = {}

    def _work():
        try:
            out["res"] = fn()
        except Exception as e:  # noqa: BLE001 - propagated to caller
            out["err"] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print("[chart] %s timed out after %ss (MT5 not responding)" % (what, timeout))
        raise RuntimeError(
            "%s timed out - MT5 is not responding (terminal may still be syncing)" % what)
    if "err" in out:
        raise out["err"]
    return out["res"]


# Deep tick pulls (up to 1M ticks for 24h of 15s bars) can take many seconds
# on a healthy terminal and hang indefinitely on a dead one.
_TICK_PULL_TIMEOUT = 45


def _tickbars_bounded(symbol, sec, n_bars, timeout=None):
    """get_tickbars that refuses to hang the request when MT5 goes quiet.

    `timeout` is a parameter so the background renko-base pull can
    wait longer (it's off the request path) than an interactive request.
    None means the module default (kept dynamic so tests can shrink it).
    """
    if timeout is None:
        timeout = _TICK_PULL_TIMEOUT
    return _mt5_bounded(lambda: feed.get_tickbars(symbol, sec, n_bars),
                        timeout, "tick history pull")


# crypto trades 24/7 - weekend holes in its history ARE gaps.
# Forex/metals do not trade Sat/Sun - their weekend holes are skipped.
_CRYPTO_BASES = frozenset([
    "BTC", "ETH", "LTC", "BCH", "XRP", "ADA", "DOGE", "SOL", "DOT", "LINK",
    "AVAX", "MATIC", "XLM", "ATOM",
])
_CRYPTO_QUOTES = ("USD", "USDT", "EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD")


def _trades_weekends(symbol):
    """True if the symbol trades Sat/Sun (crypto). Matches the base
    currency, case-insensitive: BTCUSD, BTC_USD (OANDA instrument),
    BTCUSD# / BTCUSD.m (MT5 suffixes). XAUUSD/forex return False."""
    s = (symbol or "").strip().upper()
    if not s:
        return False
    for sep in ("#", "."):
        if sep in s:
            s = s.split(sep, 1)[0]
    if "_" in s:
        return s.split("_", 1)[0] in _CRYPTO_BASES
    for base in _CRYPTO_BASES:
        if s.startswith(base):
            rest = s[len(base):]
            if rest == "" or any(rest.startswith(q) for q in _CRYPTO_QUOTES):
                return True
    return False


def _scan_gaps(bars, tf_sec, skip_weekends=True):
    """list every interior gap as (start_ts, end_ts) slot ranges.

    A gap = expected bar slots between the first and last bar that are
    absent. Weekend slots are skipped by default (no XAUUSD bars Sat/Sun).
    The tail (after the last bar) is NOT a gap - that's _deep_history's
    tail-merge job."""
    import datetime as _dt
    gaps = []
    if not bars or not tf_sec or len(bars) < 2:
        return gaps
    try:
        have = {b["time"] for b in bars}
        t, end = bars[0]["time"], bars[-1]["time"]
        rs = None
        while t < end:
            if t not in have:
                bad = False
                if skip_weekends:
                    wd = _dt.datetime.fromtimestamp(
                        t, tz=_dt.timezone.utc).weekday()
                    bad = wd >= 5
                if not bad:
                    rs = (rs[0], t) if rs else (t, t)
                elif rs:
                    gaps.append(rs)
                    rs = None
            elif rs:
                gaps.append(rs)
                rs = None
            t += tf_sec
        if rs:
            gaps.append(rs)
    except Exception:
        pass
    return gaps


def _gap_slots(gaps, tf_sec):
    """total missing bar slots across gap ranges."""
    n = 0
    for a, b in gaps:
        n += (b - a) // tf_sec + 1
    return n


def _fmt_range_ist(a, b):
    """human-readable " 13:20-15:30 IST" for a gap range.

    The chart's time axis renders in the user's local timezone (IST for
    the user), so unfilled gaps are reported in IST, not raw unix seconds."""
    import datetime as _dt
    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    fa = _dt.datetime.fromtimestamp(a, tz=ist).strftime("%Y-%m-%d %H:%M")
    fb = _dt.datetime.fromtimestamp(b, tz=ist).strftime("%H:%M")
    return "%s-%s IST" % (fa, fb)


def _describe_missing(gaps, tf_sec, max_ranges=3):
    """summarize remaining gaps as (slots, [range strings]).

    Used by heal_data so the Fix-data dialog can report honestly which
    candles are still missing instead of claiming "data is complete"
    when MT5 simply returned no data for the hole."""
    slots = _gap_slots(gaps, tf_sec)
    ranges = [_fmt_range_ist(a, b) for a, b in gaps[:max_ranges]]
    if len(gaps) > max_ranges:
        ranges.append("+%d more" % (len(gaps) - max_ranges))
    return slots, ranges


def heal_data(symbol, tf, force=False):
    """full data-integrity pass for one symbol/tf.

    Clears the gap-fill throttles when forced, then runs the complete
    _deep_history pipeline (tail backfill from MT5 + interior gap fill +
    tick-vault heal + disk save) and reports what changed. Safe to call
    any time; no-ops instantly when MT5 is not live.
    Returns {"symbol","tf","live","gaps_before","gaps_after",
             "slots_healed","bars_total"}."""
    stats = {"symbol": symbol, "tf": tf, "live": bool(feed.live),
             "gaps_before": 0, "gaps_after": 0, "slots_healed": 0,
             "bars_total": 0, "tail_ok": True, "tail_err": None,
             "ranges_found": 0, "bars_added": 0, "bars_repaired": 0,
             "skipped": None,
             "missing_slots": 0, "missing_ranges": [],
             "tick_rebuilt": 0}
    try:
        if not feed.live:
            return stats
        tf_sec = _TF_SEC.get(tf)
        if not tf_sec:
            return stats
        if tf in ("1s", "15s"):
            # tick charts have no disk cache to repair - they are
            # drawn from the live tick stream (/api/tickbars). Repairing
            # them here would cache 1m bars under a tick key, so refuse
            # explicitly instead of silently doing the wrong thing.
            stats["skipped"] = "tick timeframe (live stream)"
            return stats
        gk = (symbol, tf)
        if force:
            _GAPFILL_LAST.pop(gk, None)
            _GAPFILL_FAIL.pop(gk, None)
            _GAPCHECK_LAST.pop(gk, None)
        with _DEEP_LOCK:
            entry = _DEEP.get(("deep", symbol, tf))
            before = entry["bars"] if entry else _load_disk(symbol, tf)
        if before:
            stats["gaps_before"] = len(_scan_gaps(
                before, tf_sec,
                skip_weekends=not _trades_weekends(symbol)))
            n_before = _gap_slots(_scan_gaps(
                before, tf_sec,
                skip_weekends=not _trades_weekends(symbol)), tf_sec)
        else:
            n_before = 0
        bars = _deep_history(symbol, tf)
        stats["bars_total"] = len(bars)
        # honest per-phase reporting for the progress dialog.
        dd = _DEEP_DIAG.get((symbol, tf), {})
        stats["tail_ok"] = dd.get("tail_ok", True)
        stats["tail_err"] = dd.get("tail_err")
        stats["ranges_found"] = dd.get("ranges_found", 0)
        stats["bars_added"] = dd.get("bars_added", 0)
        # reconstruction/glitch bars superseded by MT5 rates.
        stats["bars_repaired"] = dd.get("bars_repaired", 0)
        # bars rebuilt from MT5 tick history (copy_ticks_range)
        # when the rate history would not serve the range.
        stats["tick_rebuilt"] = dd.get("tick_bars_added", 0)
        gaps_after = _scan_gaps(bars, tf_sec,
                                skip_weekends=not _trades_weekends(symbol))
        stats["gaps_after"] = len(gaps_after)
        n_after = _gap_slots(gaps_after, tf_sec)
        stats["slots_healed"] = max(0, n_before - n_after)
        # report what is STILL missing, honestly. Before this,
        # the dialog only looked at slots_healed/tail_ok, so "gap found
        # but MT5 returned no data" rendered as "no gaps - data is
        # complete". The dialog now uses missing_slots/missing_ranges.
        if n_after:
            ms, mr = _describe_missing(gaps_after, tf_sec)
            stats["missing_slots"] = ms
            stats["missing_ranges"] = mr
        else:
            stats["missing_slots"] = 0
            stats["missing_ranges"] = []
        if stats["slots_healed"]:
            print("[chart] heal %s %s: %d gap(s), %d slot(s) healed" %
                  (symbol, tf, stats["gaps_before"], stats["slots_healed"]))
        return stats
    except Exception as e:
        stats["error"] = str(e)
        print("[chart] heal failed %s %s: %s" % (symbol, tf, e))
        return stats


def _data_integrity_loop():
    """background data-integrity monitor.

    Every _HEAL_LOOP_SECS (10 min) it heals every recently-used symbol/tf
    (the panes the user actually has open). The first run happens
    _HEAL_LOOP_FIRST_SECS (60s) after server start - that's the startup
    heal: by then MT5 has connected and synced, so post-sleep holes get
    repaired without any click. Failed fills retry on the next loop
    instead of waiting an hour."""
    try:
        time.sleep(_HEAL_LOOP_FIRST_SECS)
        first = True
        while True:
            try:
                if feed.live:
                    now = time.time()
                    targets = [k for k, ts in list(_HEAL_ACTIVE.items())
                               if now - ts < _HEAL_ACTIVE_TTL]
                    for sym, tf in targets:
                        heal_data(sym, tf, force=first)
                    if first and targets:
                        print("[chart] integrity: startup heal done for %s"
                              % (targets,))
                first = False
            except Exception as e:
                print("[chart] integrity loop error: %s" % e)
            time.sleep(_HEAL_LOOP_SECS)
    except Exception:
        pass


threading.Thread(target=_data_integrity_loop, daemon=True).start()


# volume backfill. Bars cached beforecarry volume 0
# ("unknown") while fresh MT5 bars carry real tick volume, so the histogram
# looked patchy - volume, gap, volume. volume==0 reliably means "unknown"
# in our pipeline (real MT5 tick_volume is >= 1; tick-built bars carry real
# tick counts; demo never reaches here). Fill the unknown runs from MT5's
# tick_volume via get_rates_range, most-recent first, bounded. Runs
# lock-free (MT5 calls never hold _DEEP_LOCK); Phase 3 saves the patched
# bars to the disk cache, so each range is healed once.
_VOL_HEAL_MAX_BARS = 6000   # max bars patched per _deep_history call
_VOL_HEAL_MAX_AGE = 90 * 86400  # don't chase volume older than this


def _heal_volume(symbol, tf, bars):
    if not bars or not feed.live:
        return bars
    if tf not in ("1m", "3m", "5m") and tf not in getattr(
            feed, "_NATIVE_TF", {}):
        return bars  # get_rates_range can't serve sub-minute TFs
    try:
        if hasattr(feed, "hist_mt5_allowed") and not feed.hist_mt5_allowed():
            return bars  # circuit breaker open - serve cache as-is
    except Exception:
        return bars
    now = time.time()
    # Contiguous runs of volume-unknown bars (newest run first).
    runs = []
    start = None
    for i, b in enumerate(bars):
        unknown = b.get("volume", 0) == 0 and \
            now - b["time"] < _VOL_HEAL_MAX_AGE
        if unknown:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(bars) - 1))
    if not runs:
        return bars
    budget = _VOL_HEAL_MAX_BARS
    for (a, z) in reversed(runs):
        if budget <= 0:
            break
        aa = max(a, z - budget + 1)  # most recent `budget` bars of the run
        try:
            # never wedge chart loading on a sick terminal -
            # same timeout budget as the MT5 gap fill.
            fresh = _mt5_bounded(
                lambda: feed.get_rates_range(symbol, tf, bars[aa]["time"],
                                             bars[z]["time"]),
                _GAPFILL_TIMEOUT_S, "volume heal")
        except Exception:
            continue  # timeout / MT5 sick - leave these bars for next time
        if fresh is None or len(fresh) == 0:
            continue
        vol_by_time = {}
        try:
            for r in fresh.itertuples():
                try:
                    vol_by_time[int(r.time)] = int(r.volume)
                except (TypeError, ValueError):
                    pass
        except Exception:
            continue
        for i in range(aa, z + 1):
            v = vol_by_time.get(bars[i]["time"])
            if v:
                bars[i]["volume"] = v
        budget -= (z - aa + 1)
    return bars


def _deep_history(symbol, tf):
    """Full deep bar list for a timeframe (oldest -> newest dicts).

    Disk cache (app/data/{symbol}_{tf}.parquet) survives restarts, so TF
    switches and app reopens are instant. Every call merges only the fresh
    tail from MT5, sized by the actual time gap - a 3-4h absence backfills
    just the missing bars. A full re-backfill happens only when the gap
    exceeds the deep target or no cache exists at all.

    _DEEP_LOCK is not held during blocking MT5 calls.
    If an MT5 call hangs (or a browser refresh cancels a
    request mid-fetch), the lock stays held forever and every subsequent
    /api/candles request blocks indefinitely -> blank chart after refresh,
    requiring a full server restart. The lock is only held for quick
    cache reads/writes; MT5 fetches run lock-free so other requests keep
    serving cached bars even if one fetch hangs.
    """
    key = ("deep", symbol, tf)
    target = _DEEP_BARS.get(tf, 600)
    tf_sec = _TF_SEC.get(tf, 60)

    # mark this series as watched so the background integrity
    # loop heals it (startup + every 10 min).
    if feed.live:
        _HEAL_ACTIVE[key[1:]] = time.time()

    # Phase 1: quick cache check (brief lock, never blocks on MT5).
    with _DEEP_LOCK:
        entry = _DEEP.get(key)
        bars = entry["bars"] if entry is not None else _load_disk(symbol, tf)
        if bars and not feed.live:
            return bars
        if bars and feed.live:
            gap = int((time.time() - bars[-1]["time"]) / tf_sec) + 10
            # don't take the fast path when the cache has interior
            # holes - fall through so _fill_mt5_gaps can repair them.
            # throttle the gap scan to once per 5 min per symbol/tf.
            # It ran on EVERY 1-second poll, filtering 20K bars each time.
            if gap <= 1 and not _has_interior_gaps_throttled(symbol, tf, bars, tf_sec):
                return bars  # fresh enough, no MT5 call needed
        # else: need MT5 fetch - release lock first (see Phase 2)

    # Phase 2: MT5 fetch WITHOUT holding _DEEP_LOCK, so a hung MT5 call
    # cannot block other requests. Concurrent fetches may duplicate work,
    # but the last writer wins and correctness is preserved.
    # FAST INITIAL LOAD. When there's no cache at all, pull just
    # 1000 bars quickly and return them immediately so the chart draws
    # instantly. The full deep backfill (20K bars) continues in a background
    # thread and updates the cache - the next /api/candles poll picks it up.
    # This makes timeframe switches feel instant instead of hanging.
    # per-call diagnostics for the Fix-data progress/completion
    # dialog (see _DEEP_DIAG). _fill_mt5_gaps fills in ranges_found /
    # bars_added on this same dict.
    diag = {"tail_ok": True, "tail_err": None, "ranges_found": 0,
            "bars_added": 0, "at": time.time()}
    # Stored BEFORE _fill_mt5_gaps runs: it mutates this same dict in place
    # (ranges_found / bars_added), and heal_data reads it afterwards.
    _DEEP_DIAG[(symbol, tf)] = diag
    # circuit breaker - when MT5 history is sick, skip every MT5
    # attempt and serve the cache instantly. Without this, each /api/candles
    # burns 30s (tail) + 30s (gapfill) of timeouts, so a browser refresh
    # "stops working" for a minute+. One probe per 5 min still goes through
    # (hist_mt5_allowed) and closes the breaker when MT5 recovers.
    _hist_allowed = (feed.hist_mt5_allowed()
                     if hasattr(feed, "hist_mt5_allowed") else True)
    try:
        if not _hist_allowed and bars:
            diag["tail_ok"] = False
            diag["tail_err"] = ("MT5 history unreachable - showing cached "
                                "bars (auto-retry in background)")
        elif not _hist_allowed:
            raise RuntimeError(
                "MT5 history unreachable and no cached data for %s %s"
                % (symbol, tf))
        elif not bars:
            # try the external history DB before hitting MT5.
            bars = _history_db_seed(symbol, tf, target)
            if not bars:
                # Fast path: 1000 bars now (chart draws instantly).
                # a failed quick pull must not kill the deep
                # backfill attempt below - fall through to it.
                try:
                    quick = _fetch_tail(symbol, tf, 1000)
                except Exception as e:
                    quick = None
                    diag["tail_ok"] = False
                    diag["tail_err"] = "quick pull failed: %s" % e
                if quick:
                    bars = quick
                    # Background: full deep load, updates cache when done.
                    def _bg_full():
                        try:
                            full = _backfill_full(symbol, tf, target)
                            if full:
                                with _DEEP_LOCK:
                                    _DEEP[key] = {"bars": full}
                                    if feed.live:
                                        _save_disk(symbol, tf, full)
                        except Exception:
                            pass
                    threading.Thread(target=_bg_full, daemon=True).start()
                else:
                    # bounded - the terminal is already sick here
                    # (quick pull failed); fall through to gap-fill / stale.
                    bars = _mt5_bounded(
                        lambda: _backfill_full(symbol, tf, target),
                        60, "deep backfill")  # raises if no MT5/demo
        elif feed.live:
            gap = int((time.time() - bars[-1]["time"]) / tf_sec) + 10
            if gap >= target:
                # a failed full backfill must NOT skip the interior
                # gap fill below (classic post-sleep case: the terminal is
                # still syncing history).
                # bounded - same reasoning as above.
                try:
                    bars = _mt5_bounded(
                        lambda: _backfill_full(symbol, tf, target),
                        60, "deep backfill")
                except Exception as e:
                    diag["tail_ok"] = False
                    diag["tail_err"] = "full backfill failed: %s" % e
                    feed.note_hist_fail()
            elif gap > 1:
                # same here - copy_rates_from_pos can return nothing
                # while copy_rates_range (used by the gap fill) already
                # works. The interior fill MUST still run.
                try:
                    fresh = _fetch_tail(symbol, tf, max(gap, 120))
                except Exception as e:
                    fresh = None
                    diag["tail_ok"] = False
                    diag["tail_err"] = "tail pull failed: %s" % e
                    feed.note_hist_fail()
                if fresh:
                    merged = {b["time"]: b for b in bars}
                    for b in fresh:
                        merged[b["time"]] = b
                    bars = [merged[k] for k in sorted(merged)][-target:]
                    feed.note_hist_ok()
            # fill any interior holes from MT5 directly (lock-free).
            # The tail merge above only reaches ~120 bars back and the tick
            # vault only covers periods the server was running - older holes
            # baked into the cache were permanent until now.
            # ALWAYS runs, even when the tail pull above failed.
            # bounded - a hung MT5 range call must never wedge
            # chart loading (blank charts + stuck startup overlay).
            bars = _fill_mt5_gaps_bounded(symbol, tf, bars, diag)
            # ranges MT5's rate history won't serve can often be
            # rebuilt from its tick history (copy_ticks_range) - real ticks
            # aggregate into exact candles. Bounded, most-recent first.
            bars = _fill_tick_history_gaps(symbol, tf, bars, diag)
            if diag["tail_ok"]:
                feed.note_hist_ok()
    except Exception:
        feed.note_hist_fail()
        diag["tail_ok"] = False
        if not diag["tail_err"]:
            diag["tail_err"] = "history fetch failed"
        _DEEP_DIAG[(symbol, tf)] = diag
        if bars:
            return bars  # serve stale cache on transient MT5 errors
        raise

    # backfill volume for bars cached before(the
    # patchy histogram). Lock-free; Phase 3 below saves the patched bars
    # so each range is healed once.
    try:
        bars = _heal_volume(symbol, tf, bars)
    except Exception:
        pass

    # Phase 3: quick cache update (brief lock).
    with _DEEP_LOCK:
        # Heal any slots MT5 skipped from our own tick recording.
        bars = _heal_gaps(symbol, tf, bars, target)
        if feed.live:
            _save_disk(symbol, tf, bars)
        _DEEP[key] = {"bars": bars}
        return bars


def _bars_to_json(df):
    # MT5 tick volume rides on every bar (0 when unknown).
    has_vol = "volume" in getattr(df, "columns", [])
    out = []
    for r in df.itertuples():
        v = 0
        if has_vol:
            try:
                vv = float(r.volume)
                v = int(vv) if vv == vv else 0  # guard NaN
            except (TypeError, ValueError):
                v = 0
        out.append({"time": int(r.time), "open": round(float(r.open), 3),
                    "high": round(float(r.high), 3),
                    "low": round(float(r.low), 3),
                    "close": round(float(r.close), 3), "volume": v})
    return out


@app.route("/")
def index():
    # Inject the server version into the app.js URL so every new release
    # busts the browser cache automatically (stale cached JS was hiding fixes).
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/app.js", "/static/app.js?v=" + VERSION)
    return Response(html, mimetype="text/html")


@app.route("/api/time_resync", methods=["POST"])
def api_time_resync():
    """Force a fresh MT5-clock-offset measurement.

    If the chart shows the wrong time, the offset may have been measured
    while MT5 was sick. This clears the cached value so the next read
    re-measures from live tick + bar clocks (cross-validated).
    """
    try:
        off = feed.resync_time() if feed.live else 0
        return jsonify({"utc_offset_h": off / 3600.0, "ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status")
def api_status():
    ms = conn.status()
    live = feed.live
    return jsonify({
        "version": VERSION,
        "mode": ("LIVE-OANDA" if _FEED_TAG_RAW == "oanda" else "LIVE-MT5")
                if live else "DEMO",
        # which price feed is serving data ("mt5" | "oanda").
        "feed_source": _FEED_TAG_RAW,
        "feed_label": "OANDA" if _FEED_TAG_RAW == "oanda" else "MT5",
        "has_mt5": HAS_MT5,
        "symbol": feed.detect_symbol() if live else "XAUUSD",
        "tz": feed.tz_label if live else "demo data (UTC)",
        "utc_offset_h": (feed.utc_offset() / 3600.0) if live else 0,
        "server_time": feed.server_time() if live else int(time.time()),
        "mt5": ms,
        # Local tick vault health seconds_24h climbing ~1/sec while
        # the server runs proves the recorder is alive.
        "tick_vault": vault.status(feed.detect_symbol() if live else None),
        # Background Alert Engine evaluates every active alert on
        # its own timeframe, browser open or not.
        "alert_engine": dict(_ENGINE_STATS, running=feed.live,
                            cycle_s=_ALERT_ENGINE_S),
        # half-dead MT5 (quotes alive, history frozen) is the
        # top suspect for frozen candles - surfaced so the UI can say so.
        "feed_degraded": bool(feed.degraded) if live else False,
        # history circuit-breaker state (closed|open|half-open).
        "hist_circuit": getattr(feed, "hist_circuit", "closed"),
    })


# STABLE SCHEMA - GET /api/health
# Feed-liveness probe. This schema is the future auto-trader's liveness
# probe: field names and types below are frozen; additive fields only.
#   {"feed": "oanda"|"mt5",
# "stream_up": bool, # stream connected (OANDA) / terminal+feed live (MT5)
# "secs_since_tick": float|null, # age of last tick; null = never seen
# "secs_since_heartbeat": float|null, # OANDA stream heartbeat age; null = n/a or never
# "quote_stale": bool, # no tick in the last 45s
# "last_bar_time": int|null, # newest cached M1 bar (unix s); null = unavailable
# "hist_sync": "running"|"idle"} # deep-history worker state
# NEVER raises: any failure collapses to nulls/False.
_HEALTH_QUOTE_STALE_S = 45  # mirrors oandafeed._QUOTE_STALE_S


def _feed_health():
    now = time.time()
    out = {"feed": _FEED_TAG_RAW, "stream_up": False,
           "secs_since_tick": None, "secs_since_heartbeat": None,
           "quote_stale": True, "last_bar_time": None, "hist_sync": "idle"}
    try:
        if _FEED_TAG_RAW == "oanda":
            tick_at = float(getattr(feed, "_tick_at", 0.0) or 0.0)
            last_beat = float(getattr(feed, "_last_beat", 0.0) or 0.0)
            out["stream_up"] = bool(getattr(feed, "_stream_up", False))
            if tick_at > 0:
                out["secs_since_tick"] = now - tick_at
            if last_beat > 0:
                out["secs_since_heartbeat"] = now - last_beat
            out["quote_stale"] = (out["secs_since_tick"] is None or
                                  out["secs_since_tick"] > _HEALTH_QUOTE_STALE_S)
        else:
            # MT5 has no continuous tick clock server-side: one light
            # symbol_info_tick probe stands in for tick age.
            try:
                out["stream_up"] = bool(feed.live)
            except Exception:
                out["stream_up"] = False
            if out["stream_up"]:
                try:
                    import mt5 as _mt5  # noqa
                    sym = "XAUUSD"
                    tk = _mt5.symbol_info_tick(sym)
                    if tk is not None:
                        ts = feed.to_utc(int(tk.time))
                        out["secs_since_tick"] = now - ts
                        out["quote_stale"] = (out["secs_since_tick"] >
                                              _HEALTH_QUOTE_STALE_S)
                    else:
                        out["quote_stale"] = True
                except Exception:
                    out["quote_stale"] = True
            else:
                out["quote_stale"] = True
            # heartbeats are an OANDA-stream concept - null in MT5 mode.
        # Newest cached M1 bar (best-effort; archive meta sidecar avoids
        # reading the multi-million-row parquet).
        try:
            meta = _archive_meta("XAUUSD")
            if meta and meta.get("last"):
                out["last_bar_time"] = int(meta["last"])
        except Exception:
            pass
        # Deep-history worker state.
        try:
            out["hist_sync"] = "running" if _DEEP_SYNC_ACTIVE else "idle"
        except Exception:
            pass
    except Exception:
        pass
    return out


@app.route("/api/health")
def api_health():
    try:
        return jsonify(_feed_health())
    except Exception:
        return jsonify({"feed": "mt5", "stream_up": False,
                        "secs_since_tick": None, "secs_since_heartbeat": None,
                        "quote_stale": True, "last_bar_time": None,
                        "hist_sync": "idle"})


# ------------------------------------------------------ Backfill / backup / restore
@app.route("/api/backfill/start", methods=["POST"])
def api_backfill_start():
    """Start a manual deep backfill (M1 + ticks) in the background."""
    _bf = _imp("backfill")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol") or request.args.get("symbol") or feed.symbol
    tick_days = int(data.get("tick_days", 15))
    jid = _bf.start_backfill(symbol, feed, tick_days)
    return jsonify({"job_id": jid})


@app.route("/api/backfill/status")
def api_backfill_status():
    _bf = _imp("backfill")
    jid = request.args.get("job_id", "")
    return jsonify(_bf.job_status(jid))


@app.route("/api/history_status")
def api_history_status():
    """status of the external standalone history database.

    ?refresh=1 forgets the cached handle so a newly dropped-in .db file
    is picked up without restarting the server.
    """
    try:
        _hdb = _imp("history_db")
        if request.args.get("refresh"):
            _hdb.refresh()
        return jsonify(_hdb.get_db().status())
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


# tick data lifecycle - gap backfill, incremental backups, merge.
@app.route("/api/ticks/gap")
def api_ticks_gap():
    _tm = _imp("tick_manager")
    return jsonify(_tm.tick_gap())


@app.route("/api/ticks/backfill", methods=["POST"])
def api_ticks_backfill():
    _tm = _imp("tick_manager")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    jid = _tm.start_gap_backfill(symbol, feed)
    return jsonify({"job_id": jid})


@app.route("/api/ticks/backup_incr", methods=["POST"])
def api_ticks_backup_incr():
    _tm = _imp("tick_manager")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    start = data.get("start_date", "")
    end = data.get("end_date", "")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required "
                                 "(YYYY-MM-DD)"}), 400
    jid = _tm.start_incr_backup(symbol, feed, start, end)
    return jsonify({"job_id": jid})


@app.route("/api/ticks/incr_list")
def api_ticks_incr_list():
    _tm = _imp("tick_manager")
    return jsonify({"files": _tm.list_incr_backups()})


@app.route("/api/ticks/merge_incr", methods=["POST"])
def api_ticks_merge_incr():
    _tm = _imp("tick_manager")
    data = request.get_json(force=True, silent=True) or {}
    files = data.get("files") or [f["file"] for f in _tm.list_incr_backups()]
    jid = _tm.start_merge_incr(files)
    return jsonify({"job_id": jid})


@app.route("/api/ticks/job/<jid>")
def api_ticks_job(jid):
    _tm = _imp("tick_manager")
    return jsonify(_tm.job_status(jid))


@app.route("/api/browse")
def api_browse():
    """server-side folder browser for picking the .db file
    (browsers can't reveal real paths via <input type=file>).
    drive list via GetLogicalDrives (instant) - probing A:\\..Z:\\
    with isdir can hang for 30s+ on disconnected network drives.
    simplified - no drive enumeration at all. The frontend starts
    at C:\\stocksophist\\data (or C:\\) and navigates from there."""
    req = (request.args.get("path") or "").strip()
    try:
        if not req:
            # Default start: the expected data folder, else C:\, else /.
            for cand in (r"C:\stocksophist\data", r"C:\\", "/"):
                if os.path.isdir(cand):
                    req = cand
                    break
        p = os.path.abspath(req)
        if not os.path.isdir(p):
            return jsonify({"error": "Not a folder: %s" % req}), 400
        dirs, files = [], []
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs.append(e.name)
                        elif e.is_file(follow_symlinks=False) and \
                                e.name.lower().endswith(".db"):
                            files.append({"name": e.name,
                                          "size_mb": round(e.stat().st_size / 1e6, 1)})
                    except Exception:
                        continue
        except PermissionError:
            return jsonify({"error": "Access denied: %s" % p}), 403
        dirs.sort(key=str.lower)
        files.sort(key=lambda f: f["name"].lower())
        parent = os.path.dirname(p)
        return jsonify({"path": p, "dirs": dirs, "files": files,
                        "parent": parent if parent != p else None,
                        "roots": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/version")
def api_version():
    """frontend checks this on load - a mismatch means the Python
    process is older than the opened page (stale server)."""
    return jsonify({"version": VERSION})


@app.route("/api/db_path", methods=["GET", "POST"])
def api_db_path():
    """where the app looks for the history database.
    POST {"path": "..."} saves a manual override (persisted in
    settings.json); POST {"path": ""} clears it. GET includes a debug
    listing of every searched folder."""
    try:
        _hdb = _imp("history_db")
        _tm = _imp("tick_manager")
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            p = (data.get("path") or "").strip().strip('"')
            s = _load_settings()
            if p:
                if not os.path.isfile(p):
                    return jsonify({"ok": False,
                                    "error": "File not found: %s" % p})
                s["db_path_override"] = p
                _save_settings(s)
            else:
                s.pop("db_path_override", None)
                _save_settings(s)
            _hdb.refresh()
            found = _hdb.find_database()
            return jsonify({"ok": True, "db_path": found,
                            "db_found": bool(found)})
        if request.args.get("refresh"):
            _hdb.refresh()
        found = _hdb.find_database()
        return jsonify({
            "data_dir": _tm._data_dir(),
            "db_path": found,
            "db_found": bool(found),
            "debug": _hdb.debug_search(),
        })
    except Exception as e:
        return jsonify({"data_dir": "", "db_path": None,
                        "db_found": False, "error": str(e)})


@app.route("/api/ticks/import_parquet", methods=["POST"])
def api_ticks_import_parquet():
    """Upload a tick parquet file (from fetch_ticks.py) -> main DB."""
    _tm = _imp("tick_manager")
    import tempfile
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".parquet")
    try:
        f.save(tmp.name)
        tmp.close()
        added, msg = _tm.import_tick_parquet(tmp.name)
        return jsonify({"added": added, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass




@app.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    """Export all cached history to a single timestamped .db file."""
    _bf = _imp("backfill")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol") or request.args.get("symbol") or feed.symbol
    try:
        path = _bf.create_backup(symbol, feed)
        return jsonify({"ok": True, "file": os.path.basename(path),
                        "path": path,
                        "size_mb": round(os.path.getsize(path) / 1e6, 1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backup/list")
def api_backup_list():
    _bf = _imp("backfill")
    return jsonify({"backups": _bf.list_backups()})


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Restore a backup file, then gap-fill missing ranges from MT5."""
    _bf = _imp("backfill")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol") or request.args.get("symbol") or feed.symbol
    fname = data.get("file", "")
    # Only allow files from our own backup dir (no path traversal).
    path = os.path.join(_bf._backup_dir(), os.path.basename(fname))
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "Backup file not found"}), 404
    jid = _bf.restore_backup(path, symbol, feed)
    return jsonify({"job_id": jid})


@app.route("/api/import/history", methods=["POST"])
def api_import_history():
    """Upload a CSV/TXT 1m history file and merge it into the database."""
    _bf = _imp("backfill")
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    f = request.files["file"]
    symbol = request.form.get("symbol") or feed.symbol
    tz = request.form.get("tz", "UTC")
    # Save to a temp location first.
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False,
                                      suffix=os.path.splitext(f.filename)[1])
    f.save(tmp.name)
    tmp.close()
    jid = _bf.import_history_csv(tmp.name, symbol, tz)
    return jsonify({"job_id": jid})


@app.route("/api/incr/status")
def api_incr_status():
    """List pending incremental files for a symbol."""
    _bf = _imp("backfill")
    symbol = request.args.get("symbol") or feed.symbol
    return jsonify({"incrementals": _bf.incr_list(symbol)})


@app.route("/api/incr/merge", methods=["POST"])
def api_incr_merge():
    """Manually merge incrementals into the main database."""
    _bf = _imp("backfill")
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol") or request.args.get("symbol") or feed.symbol
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    # merge target lives in the runtime data dir, not the code tree.
    main_path = os.path.abspath(os.path.join(DATA_DIR, f"{safe}_1m.parquet"))
    merged = _bf.incr_merge_all(symbol, main_path)
    return jsonify({"ok": True, "merged_bars": merged})


# ------------------------------------------------------ MT5 connection manager
@app.route("/api/mt5/status")
def api_mt5_status():
    return jsonify(conn.status())


@app.route("/api/mt5/detect_paths")
def api_mt5_detect_paths():
    return jsonify({"paths": detect_terminals()})


@app.route("/api/mt5/connect", methods=["POST"])
def api_mt5_connect():
    """Save settings (if remember) and (re)connect in the background.
    Poll /api/mt5/status for the result - never blocks the UI."""
    cfg = request.get_json(force=True, silent=True) or {}
    return jsonify(conn.connect_async(cfg))


@app.route("/api/mt5/disconnect", methods=["POST"])
def api_mt5_disconnect():
    return jsonify(conn.disconnect())


@app.route("/api/repair_gaps", methods=["POST"])
def api_repair_gaps():
    """manually trigger the MT5 gap-fill, bypassing the hourly
    throttle. Returns how many bars were filled."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        requested = (body.get("symbol") or request.args.get("symbol")
                     or feed.detect_symbol()).strip() or "XAUUSD"
        symbol, _ = feed.resolve_symbol(requested)
        tf = (body.get("tf") or request.args.get("tf", "1m")).strip()
        if not feed.live:
            return jsonify({"ok": False, "error": "MT5 not connected"}), 200
        # Snapshot the current cached bars (no fill yet).
        with _DEEP_LOCK:
            entry = _DEEP.get(("deep", symbol, tf))
            before = entry["bars"] if entry else _load_disk(symbol, tf)
        n_before = len(before) if before else 0
        # Bypass the hourly throttle and run a full history load (fills gaps).
        _GAPFILL_LAST.pop((symbol, tf), None)
        after = _deep_history(symbol, tf)
        n_after = len(after) if after else 0
        # Count interior gaps remaining.
        tf_sec = _TF_SEC.get(tf, 60)
        still_gaps = _has_interior_gaps(after, tf_sec, symbol) if after else False
        return jsonify({
            "ok": True, "symbol": symbol, "tf": tf,
            "bars_before": n_before, "bars_after": n_after,
            "new_bars": max(0, n_after - n_before),
            "still_has_gaps": still_gaps,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/diag/gaps")
def api_diag_gaps():
    """gap diagnostic - which slots are missing from served bars,
    what MT5 itself returns, and what the tick vault covers.
    ?tf=1m&bars=600 - reports the last N bars."""
    try:
        requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
        symbol, _ = feed.resolve_symbol(requested)
        tf = request.args.get("tf", "1m").strip()
        n = min(int(request.args.get("bars", 600)), 5000)
        tf_sec = _TF_SEC.get(tf, 60)
        all_bars = _deep_history(symbol, tf)
        sel = all_bars[-n:] if n < len(all_bars) else all_bars
        have = {b["time"] for b in sel}
        local_n = sum(1 for b in sel if b.get("local"))
        gaps = []
        if sel:
            t, end = sel[0]["time"], sel[-1]["time"]
            rs = None
            while t <= end:
                if t not in have:
                    rs = (rs[0], t) if rs else (t, t)
                elif rs:
                    gaps.append({"from": rs[0], "to": rs[1],
                                 "slots": (rs[1] - rs[0]) // tf_sec + 1})
                    rs = None
                t += tf_sec
            if rs:
                gaps.append({"from": rs[0], "to": rs[1],
                             "slots": (rs[1] - rs[0]) // tf_sec + 1})
        # What does MT5 itself serve right now for the same window?
        mt5_gaps = []
        try:
            fresh = _fetch_tail(symbol, tf, min(n, 2000))
            if fresh:
                fhave = {b["time"] for b in fresh}
                t, end = fresh[0]["time"], fresh[-1]["time"]
                rs = None
                while t <= end:
                    if t not in fhave:
                        rs = (rs[0], t) if rs else (t, t)
                    elif rs:
                        mt5_gaps.append({"from": rs[0], "to": rs[1],
                                         "slots": (rs[1] - rs[0]) // tf_sec + 1})
                        rs = None
                    t += tf_sec
                if rs:
                    mt5_gaps.append({"from": rs[0], "to": rs[1],
                                     "slots": (rs[1] - rs[0]) // tf_sec + 1})
        except Exception as e:
            mt5_gaps = [{"error": str(e)}]
        # Vault coverage for today.
        vault_info = {}
        try:
            now = int(time.time())
            day_start = (now // 86400) * 86400
            ticks = vault.fetch(symbol, day_start, now)
            vault_info = {"ticks_today": len(ticks),
                          "newest": vault.newest(symbol)}
        except Exception as e:
            vault_info = {"error": str(e)}
        return jsonify({
            "symbol": symbol, "tf": tf, "bars": len(sel),
            "earliest": sel[0]["time"] if sel else None,
            "latest": sel[-1]["time"] if sel else None,
            "served_gaps": gaps[:20], "served_gap_count": len(gaps),
            "mt5_gaps": mt5_gaps[:20], "mt5_gap_count": len(mt5_gaps),
            "vault_healed_bars": local_n, "vault": vault_info,
            "feed_live": bool(feed.live),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _parse_upto():
    """Parse the `upto` query param (unix seconds) used by bar-replay.

    Returns an int, or None when absent/invalid. A non-positive value is
    treated as absent so a bad param can never blank the chart."""
    try:
        v = int(float(request.args.get("upto", "") or 0))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@app.route("/api/candles")
def api_candles():
    requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    symbol, note = feed.resolve_symbol(requested)
    tf = request.args.get("tf", "1m").strip()
    bars = min(int(request.args.get("bars", 600)), 200000)
    if tf in ("1s", "15s"):
        return jsonify({"error": "use /api/tickbars for sub-minute timeframes"}), 400
    if tf not in _DEEP_BARS and tf not in _TF_M1_MULT:
        return jsonify({"error": f"unknown timeframe {tf}"}), 400
    # bar replay: `upto` (unix seconds) truncates the series so the
    # chart and indicators see only bars at/before the replay cursor -
    # replay can never peek at future bars.
    upto = _parse_upto()
    # instant start: fast=1 serves the disk cache only - zero MT5
    # calls, so first paint is instant even on a cold/slow terminal. The
    # client then runs the full deep load in the background and swaps.
    if request.args.get("fast") == "1":
        all_bars = _load_disk(symbol, tf) or []
    else:
        # honest JSON errors instead of a bare Flask 500 page -
        # the user learns the real reason (e.g. OANDA returned no M1 data
        # for EURUSD) instead of a bare "HTTP 500".
        try:
            all_bars = _deep_history(symbol, tf)
        except Exception as e:
            return jsonify({"error": str(e) or e.__class__.__name__,
                            "symbol": symbol, "tf": tf}), 502
    # scrolling back past the working set serves the deep archive's
    # tail (1m only) instead of silently truncating at the working set edge.
    # The meta check keeps the multi-million-row read off the request path
    # unless the archive genuinely holds more than the working set.
    if tf == "1m" and bars > len(all_bars):
        am = _archive_meta(symbol)
        if am and am.get("bars", 0) > len(all_bars):
            arch = _load_archive_tail(symbol, bars)
            if len(arch) > len(all_bars):
                all_bars = arch
    sel = all_bars[-bars:] if bars < len(all_bars) else all_bars
    if upto is not None:
        sel = [b for b in sel if b["time"] <= upto]
    return jsonify({
        "symbol": symbol, "tf": tf, "bars": sel,
        "earliest": sel[0]["time"] if sel else None,
        "total": len(all_bars),
        # freshness metadata: lets the client raise BARS STALE the
        # moment the backend serves old bars (instead of discovering it
        # minutes later via the quote clock).
        "server_time": int(time.time()),
        "as_of": sel[-1]["time"] if sel else None,
        "resolved_from": requested if note else None,
        "resolve_note": note,
    })


# Economic calendar (ForexFactory weekly JSON).
# Why this source: scraping forexfactory.com/calendar gets rate-limited /
# blocked on bulk requests. ForexFactory itself publishes its whole week's
# calendar as one small JSON file on its CDN
# (https://nfs.faireconomy.media/ff_calendar_thisweek.json, ~20KB) -
# designed for exactly this. We fetch it ONCE at startup and refresh every
# 4 hours into the server's own memory; the browser only ever reads our
# cache, so ForexFactory never sees more than ~6 requests/day from us.
# The motto is a clean "what's on today" board - no live actuals polling.
_NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_NEWS_TTL = 4 * 3600  # refresh every 4 hours
_NEWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# the same weekly JSON is also cached on DISK (runtime-only) so
# chart news markers survive restarts and work before the first fetch. It
# lives in DATA_DIR (C:\stocksophist\cache on Windows, ~/.stocksophist_chart_cache on
# Linux) - created at runtime, NEVER shipped inside the release zip (the
# zip is built from the source tree only).
_NEWS_DISK = os.path.join(DATA_DIR, "ff_calendar_thisweek.json")
_NEWS_DISK_TTL = 12 * 3600  # disk cache valid 12h
_NEWS = {"events": [], "fetched_at": 0, "ok": False, "error": None}
_NEWS_LOCK = threading.Lock()
_IST = None  # lazy timezone


def _ist_tz():
    global _IST
    if _IST is None:
        import datetime as _dt
        _IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    return _IST


def _parse_news_items(items):
    """Raw ff_calendar_thisweek.json array -> sorted event list. Shared by
    the network fetch and the disk-cache read (both are the same source)."""
    import datetime as _dt
    events = []
    for it in items or []:
        try:
            d = _dt.datetime.fromisoformat((it or {}).get("date", ""))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            ts = int(d.timestamp())
            events.append({
                "ts": ts,
                "title": (it.get("title") or "").strip(),
                "country": (it.get("country") or "").strip().upper(),
                "impact": (it.get("impact") or "").strip(),
                "forecast": (it.get("forecast") or "").strip(),
                "previous": (it.get("previous") or "").strip(),
            })
        except Exception:
            continue
    events.sort(key=lambda e: e["ts"])
    return events


def _write_news_disk(raw):
    """Persist the fetched JSON to the runtime-only disk cache (atomic)."""
    try:
        tmp = _NEWS_DISK + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, _NEWS_DISK)
    except Exception as e:
        print("[chart] news disk cache write failed: %s" % e, flush=True)


def _read_news_disk():
    """Parsed events from the disk cache, or None (missing/stale/unreadable).

    used by the chart-marker range endpoint - markers survive
    restarts and are available before the first network fetch completes."""
    try:
        if not os.path.exists(_NEWS_DISK):
            return None
        if time.time() - os.path.getmtime(_NEWS_DISK) > _NEWS_DISK_TTL:
            return None  # stale - the fetch loop will refresh it
        with open(_NEWS_DISK, "rb") as f:
            raw = f.read()
        import json as _json
        events = _parse_news_items(_json.loads(raw.decode("utf-8", "replace")))
        return events or None
    except Exception:
        return None


def _fetch_news():
    """Pull the weekly calendar JSON into memory. Keeps the old cache on
    any failure (network down, feed hiccup) - a stale board beats none."""
    import urllib.request as _url
    req = _url.Request(_NEWS_URL, headers={"User-Agent": _NEWS_UA})
    with _url.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "replace")
    import json as _json
    items = _json.loads(raw)
    events = _parse_news_items(items)
    # also persist raw JSON to the runtime-only disk cache.
    _write_news_disk(raw.encode("utf-8"))
    with _NEWS_LOCK:
        _NEWS["events"] = events
        _NEWS["fetched_at"] = int(time.time())
        _NEWS["ok"] = True
        _NEWS["error"] = None
    print("[chart] news: fetched %d events" % len(events))


def _news_loop():
    """fetch the 7-day calendar at startup, refresh every 4h.
    seeds the in-memory board from the runtime disk cache first
    (fresh <12h) so the calendar and chart markers are usable instantly,
    even before - or without - the first network fetch."""
    try:
        time.sleep(15)  # let the server finish booting first
        seeded = _read_news_disk()
        if seeded:
            with _NEWS_LOCK:
                _NEWS["events"] = seeded
                _NEWS["fetched_at"] = int(time.time())
                _NEWS["ok"] = True
            print("[chart] news: seeded %d events from disk cache" % len(seeded))
        while True:
            try:
                _fetch_news()
            except Exception as e:
                with _NEWS_LOCK:
                    _NEWS["error"] = str(e)[:120]
                print("[chart] news fetch failed: %s" % e)
            time.sleep(_NEWS_TTL)
    except Exception:
        pass


threading.Thread(target=_news_loop, daemon=True).start()


def _api_news_range(_from, _to):
    """GET /api/news?from=<unix>&to=<unix>[&medium=1].

    Returns USD High-impact events inside [from, to] (Medium included only
    when medium=1). Data comes from the runtime-only disk cache (12h TTL);
    on failure an empty list is served - this endpoint NEVER crashes and
    NEVER gates signals/alerts (display only)."""
    try:
        try:
            f = int(float(_from))
            t = int(float(_to))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "from/to must be unix timestamps"}), 400
        if t < f:
            return jsonify({"ok": False, "error": "to must be >= from"}), 400
        want_medium = (request.args.get("medium") or "").strip() == "1"
        events = _read_news_disk()
        if events is None:
            with _NEWS_LOCK:
                events = list(_NEWS["events"])
            fetched_at = _NEWS["fetched_at"]
        else:
            with _NEWS_LOCK:
                fetched_at = _NEWS["fetched_at"]
        out = []
        for e in events or []:
            try:
                ts = int(e.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts < f or ts > t:
                continue
            if (e.get("country") or "").upper() != "USD":
                continue
            impact = (e.get("impact") or "").strip()
            if impact != "High" and not (want_medium and impact == "Medium"):
                continue
            out.append({
                "ts": ts,
                "title": e.get("title") or "",
                "country": "USD",
                "impact": impact,
                "forecast": e.get("forecast") or "",
                "previous": e.get("previous") or "",
            })
        return jsonify({"ok": True, "from": f, "to": t, "medium": want_medium,
                        "events": out, "count": len(out),
                        "fetched_at": fetched_at,
                        "server_time": int(time.time())})
    except Exception as e:
        return jsonify({"ok": False, "events": [], "count": 0,
                        "error": str(e)[:200]}), 200


@app.route("/api/news")
def api_news():
    """events for one IST calendar day (default: today).

    Times are returned as IST "HH:MM" plus epoch `ts` so the client can
    do past/upcoming/countdown rendering locally.

    RANGE MODE for chart news markers - when `from`/`to` (unix
    seconds) are given, this endpoint instead returns USD high-impact
    events (plus Medium when medium=1) inside [from, to], as
    {"ok","from","to","events","count","fetched_at"}. Display only -
    markers must never gate signals or alerts."""
    # --range mode (chart news markers) ---
    _from = request.args.get("from")
    _to = request.args.get("to")
    if _from is not None or _to is not None:
        return _api_news_range(_from, _to)
    import datetime as _dt
    day = (request.args.get("date") or "").strip()
    ist = _ist_tz()
    if not day:
        day = _dt.datetime.now(ist).strftime("%Y-%m-%d")
    try:
        _dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return jsonify({"ok": False, "error": "bad date, use YYYY-MM-DD"}), 400
    with _NEWS_LOCK:
        events = list(_NEWS["events"])
        fetched_at = _NEWS["fetched_at"]
        ok = _NEWS["ok"]
        err = _NEWS["error"]
    out = []
    for e in events:
        d = _dt.datetime.fromtimestamp(e["ts"], tz=ist)
        if d.strftime("%Y-%m-%d") != day:
            continue
        out.append({
            "ts": e["ts"],
            "time": d.strftime("%H:%M"),
            "title": e["title"],
            "country": e["country"],
            "impact": e["impact"],
            "forecast": e["forecast"],
            "previous": e["previous"],
        })
    return jsonify({
        "ok": True, "date": day, "events": out,
        "count": len(out),
        "server_time": int(time.time()),
        "fetched_at": fetched_at, "feed_ok": ok, "feed_error": err,
        "stale": (int(time.time()) - fetched_at) > _NEWS_TTL if fetched_at else True,
    })


@app.route("/api/news/refresh", methods=["POST"])
def api_news_refresh():
    """force an immediate calendar re-fetch (header (refresh) button)."""
    try:
        _fetch_news()
        with _NEWS_LOCK:
            n = len(_NEWS["events"])
        return jsonify({"ok": True, "count": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502


# background heal jobs so the UI can show real progress.
# POST /api/data/heal starts one and returns {"ok": true, "job": id}
# immediately; the browser polls GET /api/data/heal/status?job=<id> for
# the current step and, when done, the per-series results.
_HEAL_JOBS = {}
_HEAL_JOB_SEQ = [0]
_HEAL_JOB_LOCK = threading.Lock()
_HEAL_JOB_TTL = 1800  # keep finished jobs 30 min for late pollers


def _heal_job_new(targets):
    with _HEAL_JOB_LOCK:
        _HEAL_JOB_SEQ[0] += 1
        jid = "heal-%d" % _HEAL_JOB_SEQ[0]
    now = time.time()
    _HEAL_JOBS[jid] = {"id": jid, "done": False, "step": "Starting...",
                       "series_done": 0, "series_total": len(targets),
                       "targets": list(targets), "results": [],
                       "total_healed": 0, "live": bool(feed.live),
                       "error": None, "at": now}
    # prune jobs older than the TTL so the dict can't grow unbounded
    for old in [k for k, j in _HEAL_JOBS.items()
                if now - j.get("at", 0) > _HEAL_JOB_TTL]:
        _HEAL_JOBS.pop(old, None)
    return jid


def _heal_job_run(jid):
    job = _HEAL_JOBS.get(jid)
    if not job:
        return
    try:
        for i, (sym, tf) in enumerate(job["targets"]):
            job["step"] = "Scanning %s %s for gaps..." % (sym, tf)
            r = heal_data(sym, tf, force=True)
            if r.get("bars_added"):
                job["step"] = ("Backfilled %d bar(s) for %s %s, "
                               "saving..." % (r["bars_added"], sym, tf))
            job["results"].append(r)
            job["series_done"] = i + 1
        job["total_healed"] = sum(x.get("slots_healed", 0)
                                  for x in job["results"])
        # total candles still missing across all series, so the
        # dialog's top line stays honest when MT5 gave no data.
        job["total_missing"] = sum(x.get("missing_slots", 0)
                                   for x in job["results"])
        # bars rebuilt from MT5 tick history across all series.
        job["total_tick_rebuilt"] = sum(x.get("tick_rebuilt", 0)
                                        for x in job["results"])
        # reconstruction/glitch bars superseded by MT5 rates.
        job["total_repaired"] = sum(x.get("bars_repaired", 0)
                                    for x in job["results"])
        job["live"] = bool(feed.live)
    except Exception as e:
        job["error"] = str(e)
        print("[chart] heal job %s failed: %s" % (jid, e))
    job["done"] = True
    job["step"] = "Done."


@app.route("/api/data/heal", methods=["POST"])
def api_data_heal():
    """manual "Fix data" - starts a background heal job and
    returns immediately so the UI can show progress.

    Body (JSON, optional): {"symbol": ..., "tf": ...}. With no body (or
    no symbol/tf), heals every recently-used series. Poll
    GET /api/data/heal/status?job=<id> for progress and results."""
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    symbol = (body.get("symbol") or "").strip()
    tf = (body.get("tf") or "").strip()
    try:
        if symbol and tf:
            targets = [(symbol, tf)]
        else:
            now = time.time()
            targets = [k for k, ts in list(_HEAL_ACTIVE.items())
                       if now - ts < _HEAL_ACTIVE_TTL]
            if not targets:
                sym = (feed.detect_symbol() or "XAUUSD").strip()
                targets = [(sym, "1m")]
        jid = _heal_job_new(targets)
        threading.Thread(target=_heal_job_run, args=(jid,),
                         daemon=True).start()
        return jsonify({"ok": True, "job": jid,
                        "series": len(targets)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/data/heal/status")
def api_data_heal_status():
    """poll a heal job started by POST /api/data/heal."""
    jid = (request.args.get("job") or "").strip()
    job = _HEAL_JOBS.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "unknown heal job"}), 404
    out = {"ok": True, "job": jid, "done": job["done"],
           "step": job["step"], "series_done": job["series_done"],
           "series_total": job["series_total"]}
    if job["done"]:
        out["results"] = job["results"]
        out["total_healed"] = job["total_healed"]
        # expose still-missing counts for the honest top line.
        out["total_missing"] = job.get("total_missing", 0)
        # bars rebuilt from MT5 tick history.
        out["total_tick_rebuilt"] = job.get("total_tick_rebuilt", 0)
        # reconstruction/glitch bars superseded by MT5 rates.
        out["total_repaired"] = job.get("total_repaired", 0)
        out["live"] = job["live"]
        if job["error"]:
            out["error"] = job["error"]
    return jsonify(out)


@app.route("/api/tickbars")
def api_tickbars():
    requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    symbol, note = feed.resolve_symbol(requested)
    try:
        sec = int(request.args.get("sec", 15))
    except ValueError:
        sec = 15
    if sec not in (1, 15):
        return jsonify({"error": "sec must be 1 or 15"}), 400
    n = min(int(request.args.get("bars", 400)), 20000)
    try:
        # bounded - a dead terminal must not wedge the pane.
        bars, quote = _tickbars_bounded(symbol, sec, n)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 501
    return jsonify({"symbol": symbol, "sec": sec, "bars": bars,
                    "earliest": bars[0]["time"] if bars else None,
                    # freshness metadata (see /api/candles).
                    "server_time": int(time.time()),
                    "as_of": bars[-1]["time"] if bars else None,
                    "quote": quote,
                    "resolved_from": requested if note else None,
                    "resolve_note": note})


def _renko_slot_bricks(bricks, offset):
    """contiguous Renko.

    lightweight-charts plots bricks on a time axis, so bricks at their real
    (gappy) formation times left visible gaps in slow periods. Remap each
    brick's `time` to its sequence slot (brick_index_in_full_series * 60) so
    bricks render evenly spaced with no time gaps. The real formation time is
    preserved in `rtime`. Brick FORMATION is untouched (renko.py) - signals,
    indicators and the alert engine read the chain directly and behave
    identically; only the chart's x-positioning changes.

    The mapping is stable across polls: `offset` is the brick's index within
    the full series (full_len - len(slice)), so an already-plotted brick
    keeps its slot as the chain grows. Returns NEW dicts - the chain's
    stored bricks are never mutated.
    """
    out = []
    for i, b in enumerate(bricks):
        nb = dict(b)
        try:
            nb["rtime"] = int(b.get("time", 0))
        except (TypeError, ValueError):
            nb["rtime"] = 0
        nb["time"] = (offset + i) * 60
        out.append(nb)
    return out


@app.route("/api/renko")
def api_renko():
    requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    symbol, note = feed.resolve_symbol(requested)
    try:
        box = float(request.args.get("box", 2.5))
    except ValueError:
        box = 2.5
    box = max(0.1, min(box, 50.0))
    bricks_n = min(int(request.args.get("bricks", 5000)), 20000)
    # `tf` selects the bar series the bricks are built from - the
    # Renko pane's timeframe. A 15s pane shows bricks built from real
    # 15-second bars (live MT5 ticks); 1m+ behaves exactly as before.
    base_tf = (request.args.get("tf", "1m") or "1m").strip()
    if base_tf not in _TF_SEC:
        base_tf = "1m"
    # bricks come from the stable incremental chain shared with
    # Renko-pane indicators (see _renko_chain) - NOT a full rebuild. A
    # rebuild on every poll re-anchored the whole series whenever the M1
    # feed revised its tail, making the visible boxes jump on each refresh.
    # bar replay: `upto` (unix seconds) rebuilds bricks from base
    # bars at/before the replay cursor and drops the forming brick, so a
    # replayed Renko chart shows exactly the closed bricks known at that
    # moment - no lookahead. The live incremental path above is untouched.
    upto = _parse_upto()
    if upto is not None:
        try:
            if base_tf in ("1s", "15s"):
                rbase = _renko_base_bars(symbol, base_tf)
                if rbase is None:
                    # replay waits for the background pull too.
                    raise _RenkoLoading(
                        "Renko bricks are still building - first load pulls "
                        "tick history in the background")
                if len(rbase) > 2000:
                    rbase = rbase[-2000:]
            else:
                rbase = _deep_history(symbol, base_tf)
            rbase = [b for b in rbase if b["time"] <= upto]
            rbricks, anchor = build_renko(rbase, box)
        except _RenkoLoading:
            return jsonify({
                "symbol": symbol, "box": box, "base_tf": base_tf,
                "loading": True, "bricks": [], "anchor": None,
                "server_time": int(time.time()),
                # the background pull's live progress (stage,
                # elapsed, stuck warnings, real error) for the overlay.
                "progress": ops.snapshot("renko:%s:%s" % (symbol, base_tf)),
            })
        except RuntimeError as e:
            # every renko-base error is an honest user-facing one
            # (dead terminal, no OANDA connection, unsupported granularity)
            # - answer 501 JSON, never a 500 HTML page that wedges the UI.
            return jsonify({"error": str(e)}), 501
        sel = [b for b in rbricks if not b.get("forming")][-bricks_n:]
        # `earliest`/`as_of` stay REAL times (the Hist badge shows a
        # date); only the plotted brick times are remapped to slots.
        earliest = sel[0]["time"] if sel else None
        as_of = sel[-1]["time"] if sel else None
        sel = _renko_slot_bricks(sel, len([b for b in rbricks
                                           if not b.get("forming")]) - len(sel))
        return jsonify({
            "symbol": symbol, "box": box, "base_tf": base_tf, "anchor": anchor,
            "bricks": sel,
            "earliest": earliest,
            "server_time": int(time.time()),
            "as_of": as_of,
            "quote": None,
            "replay_upto": upto,
            "resolved_from": requested if note else None,
            "resolve_note": note,
        })
    try:
        chain = _renko_chain(symbol, box, base_tf)
    except _RenkoLoading:
        # the first tick pull runs in the background - answer
        # immediately so the mode switch never hangs; the 5s renko poll
        # picks the bricks up when the pull finishes.
        return jsonify({
            "symbol": symbol,
            "box": box,
            "base_tf": base_tf,
            "loading": True,
            "bricks": [],
            "anchor": None,
            "server_time": int(time.time()),
            "quote": None,
            # live progress of the background tick pull.
            "progress": ops.snapshot("renko:%s:%s" % (symbol, base_tf)),
        })
    except RuntimeError as e:
        # every renko-base error is an honest user-facing one
        # (dead terminal, no OANDA connection, unsupported granularity)
        # - answer 501 JSON, never a 500 HTML page that wedges the UI.
        return jsonify({"error": str(e)}), 501
    quote = feed.get_quote(symbol)
    live = quote["bid"] if quote else None
    with _RENKO_IND_LOCK:
        bricks = chain.with_forming(live)
        anchor = chain.anchor
    sel = bricks[-bricks_n:]
    # contiguous Renko - remap to sequence slots; the real times
    # stay in `rtime` and in earliest/as_of (Hist badge).
    earliest = sel[0]["time"] if sel else None
    as_of = sel[-1]["time"] if sel else None
    sel = _renko_slot_bricks(sel, len(bricks) - len(sel))
    return jsonify({
        "symbol": symbol, "box": box, "base_tf": base_tf, "anchor": anchor,
        "bricks": sel,
        "earliest": earliest,
        # freshness metadata (see /api/candles).
        "server_time": int(time.time()),
        "as_of": as_of,
        "quote": quote,
        "resolved_from": requested if note else None,
        "resolve_note": note,
    })


@app.route("/api/ticks")
def api_ticks():
    requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    symbol, note = feed.resolve_symbol(requested)
    count = min(int(request.args.get("count", 500)), 5000)
    return jsonify({"symbol": symbol, "ticks": feed.get_ticks(symbol, count),
                    "resolved_from": requested if note else None,
                    "resolve_note": note})


@app.route("/api/quote")
def api_quote():
    requested = request.args.get("symbol", feed.detect_symbol()).strip() or "XAUUSD"
    symbol, note = feed.resolve_symbol(requested)
    tf = request.args.get("tf", "1m").strip()
    mode = request.args.get("mode", "candles").strip()
    q = feed.get_quote(symbol)
    out = {"symbol": symbol, "quote": q,
           "resolved_from": requested if note else None,
           "resolve_note": note}
    if q:
        # Every quote the server sees is recorded into the local tick vault
        # (1-second OHLC) - the raw material for gap healing.
        if feed.live:
            _VAULT_SYMBOLS.add(symbol)
            vault.record(symbol, q.get("bid"), q.get("time"))
        # Ordering guard + server clock (client drops out-of-order polls and
        # computes quote age from the same clock MT5 uses -sync).
        out["seq"] = feed.next_seq(symbol)
        out["server_time"] = feed.server_time()
        if mode == "renko":
            try:
                box = float(request.args.get("box", 2.5))
            except ValueError:
                box = 2.5
            # ONE brick series. The shared chain owns the live edge
            # (vault 1-second bars), so this poll advances exactly the same
            # bricks the chart poll, indicators and background alerts read.
            # `lastbrick` (newest brick time the client already shows) makes
            # new_bricks exact per pane.
            base_tf = tf if tf in _TF_SEC else "1m"
            bid = q.get("bid")
            try:
                lastbrick = int(request.args.get("lastbrick", 0) or 0)
            except (TypeError, ValueError):
                lastbrick = 0
            chain = None
            try:
                chain = _renko_chain(symbol, box, base_tf)
            except _RenkoLoading:
                # base still building in the background - degrade
                # to "no live bricks" exactly like the DEMO case below.
                chain = None
            except RuntimeError as e:
                # DEMO honesty 1s/15s bricks need live MT5 ticks.
                # Degrade to "no live bricks" instead of 500ing the 1s poll.
                if "live MT5" not in str(e):
                    raise
            if chain is None:
                out["renko_live"] = {"anchor": None, "forming": None,
                                     "new_bricks": []}
            else:
                with _RENKO_IND_LOCK:
                    bricks = chain.with_forming(bid)
                    anchor = chain.anchor
                    # the client sends the newest SHOWN brick time,
                    # which is a remapped slot (index*60) - convert back to a
                    # chain index for the delta. (A real-time lastbrick from
                    # an older client maps to a huge index -> empty delta,
                    # and the 5s chart poll heals it.)
                    if lastbrick > 0:
                        start_idx = lastbrick // 60 + 1
                        nb_raw = [b for idx, b in enumerate(chain.bricks)
                                  if idx >= start_idx][-100:]
                        base = len(chain.bricks) - len(nb_raw)
                        new_bricks = _renko_slot_bricks(nb_raw, base)
                    else:
                        nb_raw = chain.bricks[-100:]
                        new_bricks = _renko_slot_bricks(
                            nb_raw, len(chain.bricks) - len(nb_raw))
                forming = (bricks[-1] if bricks and bricks[-1].get("forming")
                           else None)
                if forming is not None:
                    # Forming brick's slot index in the with_forming list.
                    forming = _renko_slot_bricks(
                        [forming], len(chain.bricks))[0]
                out["renko_live"] = {"anchor": anchor, "forming": forming,
                                     "new_bricks": new_bricks}
        else:
            # Server-authoritative forming bar: the client renders exactly
            # this and never invents OHLC from poll ticks.
            out["live_bar"] = feed.get_live_bar(symbol, tf)
            # ..unless MT5's own bar froze while ticks kept flowing: then the
            # vault draws the live candle from our own recording.
            _heal_live_bar(symbol, tf, out.get("live_bar"), q)
    return jsonify(out)


# ------------------------------------------------------------ indicators
_SEL_TO_TF = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "60": "1H",
              "240": "4H", "1D": "D", "1W": "W", "1M": "M"}
_CHART_TF_MIN = {"1s": 1 / 60, "15s": 0.25, "1m": 1, "3m": 3, "5m": 5,
                 "15m": 15, "30m": 30, "1H": 60, "4H": 240, "D": 1440,
                 "W": 10080, "M": 43200}
_IND_CACHE = {}
_IND_TTL_S = 60


def _indicator_chart_bars(symbol, tf):
    """History bars for running an indicator on `tf` (shared by the
    /api/indicator route and the background Alert Engine).

    1s/15s bars reuse the cached Renko base series - one deep
    tick pull, then cheap vault tail-appends - instead of a fresh ~600k-tick
    MT5 pull on every indicator cache miss. That uncached pull was slow,
    failure-prone, and its 500s left stale Renko indicator parts stuck on
    candle panes (frontend keeps old parts when a refetch fails)."""
    if tf in ("1s", "15s"):
        return _renko_base_bars(symbol, tf)[-2000:]
    # candle-mode indicators compute on the newest 50k bars only.
    # The M1 archive can hold millions of bars now - feeding all of them
    # through the per-bar Python indicator loops would take minutes and buy
    # nothing: 50k bars (~35 days on 1m) is far more warm-up than any pivot
    # or MA state needs, and the window is stable (always the newest 50k).
    bars = _deep_history(symbol, tf)
    if len(bars) > _IND_WINDOW_BARS:
        bars = bars[-_IND_WINDOW_BARS:]
    return bars


def _renko_base_bars(symbol, base_tf):
    """Bar series that Renko bricks are built from for a base timeframe.

    a Renko pane's TF (and a Renko alert's timeframe) selects the
    base series - 15s bricks are built from real 15-second bars, not M1.
    Sub-minute bars come from live MT5 tick history in MT5 mode (one deep
    pull, cached per (symbol, TF), then cheap tail appends from our own
    1-second tick vault , so polls never re-pull thousands of
    ticks); in OANDA mode they come from OANDA's native S5/S15 candles
    via REST . Minute+ timeframes use the deep history cache as
    before. DEMO has no tick history: raises honestly (same as 15s candle
    charts) instead of silently building M1 bricks and calling them 15s.
    """
    base_tf = base_tf if base_tf in _TF_SEC else "1m"
    if base_tf not in ("1s", "15s"):
        return _deep_history(symbol, base_tf)
    sec = 1 if base_tf == "1s" else 15
    key = (symbol, base_tf)
    now = int(time.time())
    # The deep tick pull (thousands of 15s bars from tick history) can take
    # many seconds on live MT5 - it must NOT hold _RENKO_BASE_LOCK while it
    # runs, or every renko endpoint (chart poll, 1s quote, indicator,
    # background alerts) stalls behind it. Double-checked locking: fast
    # path under the lock, slow pull outside, publish under the lock.
    # the first pull does not block the request at all - it runs
    # in a background daemon thread ("in parallel"), the request returns
    # None immediately, and _renko_chain raises _RenkoLoading so the chart
    # answers {"loading": true}. The pane shows "Loading Renko bricks..."
    # and the 5s renko poll paints the bricks when the pull finishes.
    # Switching candles -> Renko never hangs the UI again.
    with _RENKO_BASE_LOCK:
        entry = _RENKO_BASE.get(key)
    if entry is None:
        with _RENKO_BASE_LOCK:
            if key not in _RENKO_BASE_LOADING:
                _RENKO_BASE_LOADING.add(key)
                threading.Thread(target=_bg_renko_base_pull,
                                 args=(symbol, base_tf, sec, key),
                                 daemon=True).start()
        return None
    if "error" in entry:
        # a failed background pull stays honest for 5 minutes, then
        # the entry is dropped so the next request starts a fresh pull -
        # a recovered terminal heals without a server restart.
        if time.monotonic() - entry.get("at", 0) < _RENKO_BASE_FAIL_BACKOFF:
            raise RuntimeError(entry["error"])
        with _RENKO_BASE_LOCK:
            _RENKO_BASE.pop(key, None)
        return _renko_base_bars(symbol, base_tf)
    with _RENKO_BASE_LOCK:
        bars = entry["bars"]
        if bars:
            try:
                tail = vault.build_bars(symbol, int(bars[-1]["time"]), now, sec)
                fresh = [b for b in tail if int(b["time"]) > int(bars[-1]["time"])]
                if fresh:
                    merged = bars + fresh
                    if len(merged) > _RENKO_BASE_MAX:
                        merged = merged[-_RENKO_BASE_MAX:]
                    entry["bars"] = merged
                    bars = merged
            except Exception:
                pass  # serve the deep pull; tail catches up next call
        return bars


def _bg_renko_base_pull(symbol, base_tf, sec, key):
    """the first deep tick pull for a renko base, off the request
    path. Runs in a daemon thread spawned by _renko_base_bars; on success
    the bars are published to _RENKO_BASE (the 5s renko poll picks them up),
    on failure an honest error is cached for 5 minutes.

    reports to the ops bus with a watchdog ticker - the pull is
    one blocking MT5 call, so a second thread narrates elapsed time and
    flags "stuck" after 30s. The UI shows this instead of a static spinner."""
    op_id = "renko:%s:%s" % (symbol, base_tf)
    n_bars = _DEEP_BARS.get(base_tf, 5760)
    # narration is feed-aware - OANDA mode pulls native S-candles,
    # not MT5 tick history.
    _is_oanda = (_FEED_TAG_RAW == "oanda")
    _src = "OANDA" if _is_oanda else "MT5"
    ops.start(op_id, "Renko %s %s - %s" % (
        symbol, base_tf,
        "S%d candle history (OANDA)" % sec if _is_oanda else "tick history"))
    stop_tick = threading.Event()

    def _ticker():
        t0 = time.monotonic()
        while not stop_tick.wait(5):
            el = int(time.monotonic() - t0)
            if el >= 30:
                ops.touch(op_id,
                          "still waiting on %s after %ds - %s; the pull "
                          "continues in the background"
                          % (_src, el,
                             "OANDA may be throttling" if _is_oanda
                             else "the terminal may be syncing"))
            else:
                ops.touch(op_id, "waiting on %s... %ds elapsed" % (_src, el))

    if _is_oanda:
        ops.stage(op_id, "Requesting S%d candle history from OANDA" % sec,
                  "asking for ~%d bars of %ds data - one OANDA REST call, no "
                  "progress %% available until it answers" % (n_bars, sec))
    else:
        ops.stage(op_id, "Requesting tick history from MT5",
                  "asking for ~%d bars of %ds data - one MT5 call, no progress "
                  "%% available until it answers" % (n_bars, sec))
    threading.Thread(target=_ticker, daemon=True).start()
    try:
        # Background: allowed longer than an interactive request (120s) -
        # the pane already shows "Loading Renko bricks..." meanwhile.
        bars, _ = _tickbars_bounded(symbol, sec, n_bars, timeout=120)
        stop_tick.set()
        ops.stage(op_id, "Building bricks", "%d bars received" % len(bars))
        with _RENKO_BASE_LOCK:
            _RENKO_BASE[key] = {"bars": bars}
        ops.done(op_id, "ready: %d %ds bars" % (len(bars), sec))
        print("[chart] renko base %s %s ready: %d bars (background pull)"
              % (symbol, base_tf, len(bars)), flush=True)
    except Exception as e:
        stop_tick.set()
        with _RENKO_BASE_LOCK:
            _RENKO_BASE[key] = {"error": str(e), "at": time.monotonic()}
        ops.fail(op_id, "%s - retrying automatically in 5 minutes" % e)
        print("[chart] renko base %s %s background pull failed: %s"
              % (symbol, base_tf, e), flush=True)
    finally:
        stop_tick.set()
        with _RENKO_BASE_LOCK:
            _RENKO_BASE_LOADING.discard(key)


def _renko_chain(symbol, box, base_tf="1m"):
    """Stable incremental RenkoChain shared by the Renko chart (/api/renko)
    and Renko-pane indicators.

    rebuilding the whole brick series from scratch on
    every poll (build_renko) re-anchors every later brick on any revision
    of the M1 tail (tick-vault healing vs MT5 recovery routinely revise
    it), so the visible boxes jump on each refresh. The chain fixes that: once a
    brick closes it is immutable; new M1 bars only APPEND; interior revisions
    are ignored. Chart and indicator always see the same bricks.

    `base_tf` selects the bar series bricks are built from (the
    Renko pane's TF / the Renko alert's timeframe). Chains are keyed per
    (symbol, box, base_tf) so a 15s-brick series never shares state with
    the 1m-brick series.

    the chain owns the live edge. After the bar update, completed
    1-second bars from the tick vault are consumed in tick order
    (RenkoChain.update_ticks), so bricks close from real tick order at
    1-second resolution. The chart, Renko-pane indicators and the background
    Alert Engine all read this one series - the old per-second tracker (its
    own anchor, wiped by every chart poll and re-emitted: the blinking last
    leg) is gone.
    """
    try:
        box = float(box)
    except (TypeError, ValueError):
        box = 2.5
    box = max(0.1, min(box, 50.0))
    base_tf = base_tf if base_tf in _TF_SEC else "1m"
    key = (symbol, round(box, 3), base_tf)
    now = time.time()
    now_sec = int(now)
    # Fetch the base bars BEFORE taking _RENKO_IND_LOCK: the first 15s/1s
    # call does a deep MT5 tick pull (seconds), and holding the chain lock
    # across it would stall the 1s quote poll, the indicator and background
    # alerts. _renko_base_bars has its own lock discipline.
    # the first pull runs in the background - _renko_base_bars
    # returns None while it does, and the chain refuses to build on nothing.
    bars = _renko_base_bars(symbol, base_tf)
    if bars is None:
        raise _RenkoLoading(
            "Renko bricks are still building - first load pulls tick "
            "history in the background")
    with _RENKO_IND_LOCK:
        chain = _RENKO_IND_CHAINS.get(key)
        if chain is None or abs(chain.box - round(box, 3)) > 1e-9:
            chain = RenkoChain(round(box, 3), _TF_SEC[base_tf])
            # seed from the disk brick cache when it is valid -
            # instant even for a multi-million-bar archive. Otherwise build
            # from the working set as before and queue the full-depth brick
            # build in the background.
            seeded = _seed_chain_from_brick_cache(chain, symbol,
                                                  round(box, 3), base_tf)
            if seeded:
                # The cache ends at the archive's last bar; brick the working
                # set's newer bars on top BEFORE the tick path takes
                # ownership below - update() is idempotent (skips anything
                # at/under last_bar_t and the forming bucket).
                chain.update(bars, now)
            if not seeded:
                chain.rebuild(bars, now)
                if base_tf == "1m" and feed.live:
                    _BRICK_WANT.add((symbol, round(box, 3), base_tf))
                    threading.Thread(
                        target=_build_brick_cache,
                        args=(symbol, round(box, 3), base_tf),
                        daemon=True).start()
            # The tick path owns every second from the first unconsumed one
            # on; history before that belongs to the bars.
            chain.last_tick_t = chain.last_bar_t + chain.sec - 1
            chain.tick_from = chain.last_bar_t + chain.sec
            _RENKO_IND_CHAINS[key] = chain
            fresh = True
        else:
            fresh = False
        # Live edge BEFORE the bar update : consume completed 1-second
        # bars in tick order first, so chain.update() below sees the
        # just-completed base bar as tick-covered and skips it instead of
        # OHLC-replaying it (which double-bricked the bar at every 15s
        # boundary: once in true tick order, once from the replay).
        # Live MT5 only; DEMO has no tick vault.
        if feed.live:
            catchup_floor = now_sec - MAX_TICK_CATCHUP_S
            floor = max(chain.last_tick_t + 1, catchup_floor)
            if catchup_floor > chain.tick_from:
                # Unfillable gap (server was down longer than the catchup
                # window): those seconds can never be tick-consumed, so tick
                # ownership restarts here. Bars before this fall back to
                # OHLC; tick_from must NOT advance in the normal case, or
                # just-completed bars fail the bt >= tick_from skip check
                # and get double-bricked.
                chain.tick_from = catchup_floor
            if floor < now_sec:
                try:
                    secs = vault.fetch(symbol, floor, now_sec - 1)
                except Exception:
                    secs = []
                if secs:
                    chain.update_ticks(secs, now_sec)
        if not fresh:
            # If the base history does not cover what the chain consumed
            # (truncated/reset history), rebuild; otherwise append only.
            # Interior revisions are deliberately ignored - closed bricks
            # never change.
            covered = False
            for b in bars:
                if int(b["time"]) >= chain.last_bar_t:
                    covered = True
                    break
            if covered:
                chain.update(bars, now)
            # else: transient feed gap - keep serving the stable chain as-is.
            # Rebuilding from a short history would destroy closed bricks.
        return chain


def _indicator_renko_bricks(symbol, box, base_tf="1m"):
    """Renko bricks for indicator computation when the pane is in Renko mode.

    The Renko chart (api_renko) is built from base-TF bars plus the live
    quote. An indicator on a Renko pane must see EXACTLY those bricks:
    the script this ports was written for a Renko chart, where close > open
    is a brick color - feeding it candles makes brick-denominated logic
    (e.g. the 2-green-box confirmation) evaluate on the wrong series and
    plot signals the bricks never confirmed.

    bricks come from a stable incremental chain - once a brick
    closes it never changes, so signals plotted on closed bricks can never
    flicker ("coming and going") when the M1 feed is revised underneath.
    `base_tf` selects the brick base series (pane TF / alert TF).
    """
    chain = _renko_chain(symbol, box, base_tf)
    with _RENKO_IND_LOCK:
        quote = feed.get_quote(symbol)
        live = quote["bid"] if quote else None
        bricks = chain.with_forming(live)
    # The port emulates barstate.isconfirmed with `confirmed = i < n - 1`,
    # so it needs the forming brick as the last element. build_renko only
    # appends one when the live price moved off the anchor; synthesize a
    # zero-size forming brick at the anchor when it didn't.
    if bricks and not bricks[-1].get("forming"):
        last = bricks[-1]
        bricks.append({
            "time": int(last["time"]) + 1, "open": last["close"],
            "high": last["close"], "low": last["close"],
            "close": last["close"], "forming": True, "dir": 0,
        })
    return bricks


def _run_indicator(ind_id, symbol, tf, settings, mode="candles", box=2.5,
                  upto=None):
    """Compute an indicator exactly like /api/indicator does (with the same
    60s result cache), returning the result dict. Raises on failure.

    mode="renko" computes on Renko bricks (built exactly like the Renko
    chart: base-TF bars + live quote) instead of candle bars - required for any
    indicator whose logic is brick-denominated. In renko mode `tf` doubles as
    the brick base timeframe a 15s Renko alert evaluates on bricks
    built from 15-second bars.

    `upto` (unix seconds, bar-replay) restricts every bar series the
    indicator can see - chart bars, Renko bricks AND the ctx["get_bars"]
    MTF helper - to time <= upto, so replayed signals can never peek ahead.
    """
    mod = indlib.get_indicator(ind_id)
    if mod is None:
        raise RuntimeError("unknown indicator: %s" % ind_id)
    merged = indlib.default_inputs(mod)
    merged.update(settings or {})
    try:
        box = float(box)
    except (TypeError, ValueError):
        box = 2.5
    mode = "renko" if str(mode) == "renko" else "candles"
    settings_key = json.dumps(merged, sort_keys=True)

    def get_bars(api_tf):
        bars = _deep_history(symbol, api_tf)
        if upto is not None:
            bars = [b for b in bars if b["time"] <= upto]
        return bars

    # for Renko the cache key includes the newest CLOSED brick time,
    # so a newly closed brick busts the cache immediately instead of waiting
    # out the 60s TTL - a 15s Renko alert must see the new brick promptly.
    # The chain makes this cheap (closed bricks are cached server-side).
    chart_bars = None
    renko_brick_t = 0
    if mode == "renko":
        base_tf = tf if tf in _TF_SEC else "1m"
        if upto is not None:
            # Bar-replay: fresh deterministic rebuild on base bars <= upto,
            # closed bricks only - the same brick series the replayed Renko
            # chart shows (see /api/renko), never the live incremental chain.
            if base_tf in ("1s", "15s"):
                rbase = _renko_base_bars(symbol, base_tf)
                if rbase is None:
                    raise _RenkoLoading(
                        "Renko bricks are still building - first load pulls "
                        "tick history in the background")
                if len(rbase) > 2000:
                    rbase = rbase[-2000:]
            else:
                rbase = _deep_history(symbol, base_tf)
            rbase = [b for b in rbase if b["time"] <= upto]
            rbricks, _anchor = build_renko(rbase, box)
            chart_bars = [b for b in rbricks if not b.get("forming")]
        else:
            chart_bars = _indicator_renko_bricks(symbol, box, base_tf)
        closed_rb = [b for b in chart_bars if not b.get("forming")]
        if closed_rb:
            renko_brick_t = int(closed_rb[-1]["time"])
    cache_key = (ind_id, symbol, tf, mode, round(box, 3), settings_key,
                 renko_brick_t, upto)
    now = time.time()
    # the in-memory entry must also match the current bar
    # signature. Without this, a result computed <=60s ago on the previous
    # bars was served (and then stored in the disk cache under the NEW
    # signature) right after a bar closed - the just-closed bar never got
    # signal evaluation and its marker appeared a candle late. The disk
    # signature read is cheap (time column only, no MT5).
    bars_sig = None
    if mode != "renko" and upto is None:
        try:
            bars_sig = _ind_cache_sig(symbol, tf)
        except Exception:
            bars_sig = None
    hit = _IND_CACHE.get(cache_key)
    if hit and now - hit[0] < _IND_TTL_S:
        if bars_sig is None or hit[2] == bars_sig:
            return hit[1]

    if chart_bars is None:
        chart_bars = _indicator_chart_bars(symbol, tf)
        if upto is not None:
            chart_bars = [b for b in chart_bars if b["time"] <= upto]
    import pandas as pd
    df = pd.DataFrame(chart_bars)
    ctx = {"symbol": symbol, "tf": tf, "bars": df, "inputs": merged,
           "minutes": _CHART_TF_MIN.get(tf, 1), "get_bars": get_bars}
    result = mod.compute(ctx)
    if not isinstance(result, dict) or "lines" not in result:
        raise RuntimeError("indicator returned a bad result")
    result.update({"symbol": symbol, "tf": tf, "mode": mode, "box": box,
                   "computed_at": int(now)})
    _IND_CACHE[cache_key] = (now, result, bars_sig)
    return result


@app.route("/api/indicators")
def api_indicators():
    """Indicator library catalogue (favorites / mine / built-in)."""
    inds, errs = indlib.list_indicators()
    return jsonify({"indicators": inds, "errors": errs})


@app.route("/api/indicator/<ind_id>")
def api_indicator(ind_id):
    mod = indlib.get_indicator(ind_id)
    if mod is None:
        return jsonify({"error": "unknown indicator"}), 404
    symbol = request.args.get("symbol", "XAUUSD").strip() or "XAUUSD"
    tf = request.args.get("tf", "1m")
    # Renko mode: the indicator must compute on bricks, not candles
    # (the script it ports runs on a Renko chart).
    mode = request.args.get("mode", "candles")
    try:
        box = float(request.args.get("box", 2.5))
    except (TypeError, ValueError):
        box = 2.5
    try:
        settings = json.loads(request.args.get("settings", "{}"))
    except Exception:
        settings = {}
    # bar replay: `upto` (unix seconds) - indicator sees only bars
    # at/before the replay cursor.
    upto = _parse_upto()
    # instant start: when the disk bars haven't moved since the
    # cached result was computed, serve it instantly - no MT5, no compute.
    # Renko-mode (brick chain, not disk bars) and replay cursors skip it.
    ind_key = _ind_cache_key(ind_id, symbol, tf, mode, box, settings, upto)
    ind_sig = _ind_cache_sig(symbol, tf)
    if mode != "renko" and not upto:
        hit = _ind_cache_get(ind_key, ind_sig)
        if hit is not None:
            return jsonify(hit)
    # overall deadline + op narration. The history pieces inside
    # are individually bounded , but several can stack on a
    # sick terminal - the endpoint as a whole must still answer. A worker
    # wedged in a C-level MT5 call can't be killed; it lingers as a daemon
    # while the request gets an honest 503.
    op_id = "ind-%s-%d" % ("".join(c for c in ind_id
                                   if c.isalnum() or c in "-_")[:24],
                           int(time.time() * 1000) % 1000000)
    ops.start(op_id, "Computing %s on %s %s" % (ind_id, symbol, tf))
    ops.stage(op_id, "Loading history & computing...")
    try:
        result = ops.run_deadline(
            lambda: _run_indicator(ind_id, symbol, tf, settings,
                                   mode=mode, box=box, upto=upto),
            120, "Indicator %s" % ind_id)
        ops.done(op_id, "Done")
    except ops.DeadlineExceeded as e:
        ops.fail(op_id, str(e))
        return jsonify({"error": str(e)}), 503
    except _RenkoLoading:
        ops.done(op_id, "Renko bricks still building - client will retry")
        # the renko base is building in the background - the
        # client's poll retries and paints the indicator when ready.
        return jsonify({"loading": True})
    except RuntimeError as e:
        ops.fail(op_id, str(e))
        return jsonify({"error": str(e)}), (501 if "live MT5" in str(e) else 500)
    except Exception as e:
        ops.fail(op_id, "indicator failed: %s" % e)
        return jsonify({"error": f"indicator failed: {e}"}), 500
    # instant start: refresh the disk cache with the just-computed
    # result. Re-read the signature AFTER the compute - _deep_history may
    # have pulled new bars and rewritten the disk cache mid-compute.
    if mode != "renko" and not upto:
        _ind_cache_put(ind_key, _ind_cache_sig(symbol, tf), result)
    return jsonify(result)


@app.route("/api/indicators/add", methods=["POST"])
def api_indicators_add():
    body = request.get_json(force=True, silent=True) or {}
    ok, msg = indlib.add_custom(body.get("id", ""), body.get("name", "Untitled"), body.get("code", ""))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/indicators/delete", methods=["POST"])
def api_indicators_delete():
    body = request.get_json(force=True, silent=True) or {}
    ok, msg = indlib.delete_custom((body.get("id") or "").strip())
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/indicators/favorite", methods=["POST"])
def api_indicators_favorite():
    body = request.get_json(force=True, silent=True) or {}
    ok = indlib.set_favorite((body.get("id") or "").strip(), bool(body.get("favorite")))
    return jsonify({"ok": ok})


# ------------------------------------------------ indicator settings
# global indicator defaults, persisted in settings.json under
# "indicator_settings" ({indicator_id: {key: value}}). The dialog saves
# here whenever a setting changes, so a changed setting applies
# immediately AND survives restarts; new instances start from these
# defaults. Per-instance tweaks still live in the pane state (local
# chart state) and win for that instance. Reset deletes the stored
# override, restoring the factory defaults from the indicator's INPUTS.
# All validation lives in indlib (pure, unit-tested); these routes are
# thin wrappers over it.
@app.route("/api/indicator_settings")
def api_indicator_settings_get():
    s = _load_settings()
    saved = s.get("indicator_settings") or {}
    if not isinstance(saved, dict):
        saved = {}
    return jsonify({"ok": True, "indicator_settings": saved})


@app.route("/api/indicator_settings", methods=["POST"])
def api_indicator_settings_post():
    body = request.get_json(force=True, silent=True) or {}
    ind_id = str(body.get("indicator") or "").strip()
    if not ind_id:
        return jsonify({"error": "indicator required"}), 400
    s = _load_settings()
    saved = s.get("indicator_settings") or {}
    if not isinstance(saved, dict):
        saved = {}
    new_saved, payload, status = indlib.apply_indicator_settings(
        saved, ind_id, body)
    if status == 200:
        s["indicator_settings"] = new_saved
        try:
            _save_settings(s)
        except Exception as e:
            return jsonify({"error": "could not save settings: %s" % e}), 500
    return jsonify(payload), status


# ------------------------------------------------------------ watchlist
@app.route("/api/prev_close")
def api_prev_close():
    symbol = (request.args.get("symbol") or "").strip() or feed.detect_symbol()
    return jsonify({"symbol": symbol, "prev_close": feed.prev_close(symbol)})


@app.route("/api/watchlist")
def api_watchlist():
    # feed_label so the footer badge says LIVE OANDA in OANDA mode.
    return jsonify({"live": feed.live, "watchlist": feed.get_watchlist(),
                    "feed_label": _FEED_NAME})


def _persist_oanda_watchlist():
    """OANDA watchlist additions persist in settings.json
    (oanda_extra_symbols) so they survive restarts. No-op for MT5 mode
    (Market Watch owns that list) and for feeds without extra_symbols()."""
    extras = getattr(feed, "extra_symbols", None)
    if not callable(extras):
        return
    try:
        s = _load_settings()
        s["oanda_extra_symbols"] = extras()
        _save_settings(s)
    except Exception:
        pass


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    sym = ((request.get_json(force=True, silent=True) or {}).get("symbol") or "").strip()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    try:
        feed.watch_add(sym)
        _persist_oanda_watchlist()
        return jsonify({"ok": True, "symbol": sym})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    sym = ((request.get_json(force=True, silent=True) or {}).get("symbol") or "").strip()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    try:
        feed.watch_remove(sym)
        _persist_oanda_watchlist()
        return jsonify({"ok": True, "symbol": sym})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


_WATCHLIST_SYNC_OP = "watchlist_sync"
_WATCHLIST_SYNC_TIMEOUT = 150  # seconds before the op honestly gives up


# Max-depth history endpoints.
@app.route("/api/history/deep", methods=["POST"])
def api_history_deep():
    """Start (or resume) a max-depth M1 sync for a symbol. Incremental: only
    the missing tail + any older bars the broker has since exposed."""
    d = request.get_json(force=True, silent=True) or {}
    sym = (d.get("symbol") or "").strip() or feed.detect_symbol()
    try:
        sym, _note = feed.resolve_symbol(sym)
    except Exception:
        pass
    queued = queue_deep_sync(sym, "manual")
    return jsonify({"ok": True, "symbol": sym, "queued": queued,
                    "note": "already syncing" if not queued else
                            "sync running in the background - watch the progress pill"})


@app.route("/api/history/deep/status")
def api_history_deep_status():
    """Progress of the deep sync + archive stats (fast: reads the .meta
    sidecar, never the big parquet)."""
    sym = (request.args.get("symbol") or "").strip() or feed.detect_symbol()
    try:
        sym, _note = feed.resolve_symbol(sym)
    except Exception:
        pass
    with _DEEP_SYNC_LOCK:
        active = sym in _DEEP_SYNC_ACTIVE
        qpos = (_DEEP_SYNC_QUEUE.index(sym) + 1) if sym in _DEEP_SYNC_QUEUE else 0
    return jsonify({"symbol": sym,
                    "op": ops.snapshot("deephist:" + sym),
                    "archive": _archive_meta(sym),
                    "active": active, "queue_pos": qpos})


@app.route("/api/history/warm", methods=["POST"])
def api_history_warm():
    """Background-warm deep history for a list of symbols (the client's
    named-profile symbols). They queue behind the active symbol; switching
    profiles later is instant."""
    d = request.get_json(force=True, silent=True) or {}
    syms = d.get("symbols") or []
    queued = []
    for s in syms:
        s = (s or "").strip()
        if not s:
            continue
        try:
            s, _note = feed.resolve_symbol(s)
        except Exception:
            pass
        if queue_deep_sync(s, "profile"):
            queued.append(s)
    return jsonify({"ok": True, "queued": queued})


@app.route("/api/renko/warm", methods=["POST"])
def api_renko_warm():
    """Register a (symbol, box, base_tf) for brick caching and kick off a
    background build when the M1 archive is ready."""
    d = request.get_json(force=True, silent=True) or {}
    sym = (d.get("symbol") or "").strip() or feed.detect_symbol()
    try:
        sym, _note = feed.resolve_symbol(sym)
    except Exception:
        pass
    try:
        box = round(float(d.get("box", 2.5)), 3)
    except (TypeError, ValueError):
        box = 2.5
    btf = d.get("base_tf") or "1m"
    if btf not in _TF_SEC:
        btf = "1m"
    _BRICK_WANT.add((sym, box, btf))
    p, _mp = _brick_cache_paths(sym, box, btf)
    if not os.path.exists(p) and btf == "1m":
        threading.Thread(target=_build_brick_cache, args=(sym, box, btf),
                         daemon=True).start()
        note = "brick build running in the background"
    else:
        note = "brick cache already present" if os.path.exists(p) else \
               "sub-minute bricks build from tick history, not cached"
    return jsonify({"ok": True, "symbol": sym, "box": box, "base_tf": btf,
                    "note": note})


@app.route("/api/watchlist/sync", methods=["POST"])
def api_watchlist_sync():
    """Sync the app's watchlist with MT5's Market Watch (MT5 is the source
    of truth). Optional {"keep": ["XAUUSD","BTCUSD"]} prunes everything else.
    Symbols the app has never cached get a background history backfill, so a
    symbol newly added in MT5 arrives with history..

    the sync runs as a background op - on a sick terminal the MT5
    calls inside can each block for tens of seconds, so this endpoint now
    returns immediately with the op id and the UI polls
    /api/ops/watchlist_sync for live stage/elapsed/stuck narration. A second
    POST while one is running just re-attaches to the running op."""
    if not feed.live:
        return jsonify({"ok": False, "error": "MT5 is not connected"}), 400
    data = request.get_json(force=True, silent=True) or {}
    # "keep" present (even []) = prune to that list; absent = plain sync.
    if isinstance(data.get("keep"), list):
        keep = [x.strip() for x in data["keep"]
                if isinstance(x, str) and x.strip()]
    else:
        keep = None
    snap = ops.snapshot(_WATCHLIST_SYNC_OP)
    if snap and snap["status"] == "running":
        return jsonify({"ok": True, "op_id": _WATCHLIST_SYNC_OP,
                        "already_running": True})
    ops.start(_WATCHLIST_SYNC_OP, "Syncing watchlist with MT5")
    done_ev = threading.Event()

    def _work():
        try:
            ops.stage(_WATCHLIST_SYNC_OP, "Contacting MT5...")
            res = feed.sync_watchlist(
                keep,
                progress=lambda s: ops.stage(_WATCHLIST_SYNC_OP, s))
            _bf = _imp("backfill")
            backfilled = []
            errors = list(res["errors"])
            try:
                cached = (set(os.listdir(DATA_DIR))
                          if os.path.isdir(DATA_DIR) else set())
            except Exception:
                cached = set()
            ops.stage(_WATCHLIST_SYNC_OP,
                      "Checking history for %d symbols..." % len(res["symbols"]))
            for sym in res["symbols"]:
                safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                               for c in sym)
                if not any(f.startswith(safe + "_") and f.endswith(".parquet")
                           for f in cached):
                    try:
                        _bf.start_backfill(sym, feed)
                        backfilled.append(sym)
                    except Exception as e:
                        errors.append("%s: backfill failed: %s" % (sym, e))
            res["backfilled"] = backfilled
            res["errors"] = errors
            res["ok"] = True
            res["count"] = len(res["symbols"])
            cur = ops.snapshot(_WATCHLIST_SYNC_OP)
            if cur and cur["status"] == "running":
                ops.done(_WATCHLIST_SYNC_OP,
                         "App watchlist now matches MT5: %d symbol%s"
                         % (res["count"],
                            "" if res["count"] == 1 else "s"),
                         result=res)
        except Exception as e:  # noqa: BLE001 - surfaced honestly via the op
            cur = ops.snapshot(_WATCHLIST_SYNC_OP)
            if cur and cur["status"] == "running":
                ops.fail(_WATCHLIST_SYNC_OP,
                         "Watchlist sync failed: %s" % e)
        finally:
            done_ev.set()

    def _watchdog():
        # Honest timeout: a terminal that never answers must surface as a
        # real error, never a silent infinite sync. (A worker wedged inside
        # a C-level MT5 call can't be killed - it lingers as a daemon while
        # the op reports the truth.)
        if not done_ev.wait(_WATCHLIST_SYNC_TIMEOUT):
            cur = ops.snapshot(_WATCHLIST_SYNC_OP)
            if cur and cur["status"] == "running":
                ops.fail(_WATCHLIST_SYNC_OP,
                         "Watchlist sync timed out after %ds - MT5 is not "
                         "responding (terminal may still be syncing). "
                         "Wait a minute and retry."
                         % _WATCHLIST_SYNC_TIMEOUT)

    threading.Thread(target=_work, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    return jsonify({"ok": True, "op_id": _WATCHLIST_SYNC_OP})


@app.route("/api/symbols/search")
def api_symbols_search():
    return jsonify({"symbols": feed.search_symbols(request.args.get("q", ""))})


def _tick_recorder_loop():
    """Record every MT5 quote into the tick vault once a second, even with no
    browser tab open. The the host machine runs 24x7, so the vault stays complete and
    any future history gap can be healed from our own recording."""
    while True:
        try:
            if feed.live:
                syms = set(_VAULT_SYMBOLS)
                syms.add((feed.detect_symbol() or "XAUUSD").strip())
                for sym in syms:
                    try:
                        q = feed.get_quote(sym)
                        if q:
                            vault.record(sym, q.get("bid"), q.get("time"))
                    except Exception:
                        pass
                vault.idle_flush()
        except Exception:
            pass
        time.sleep(1.0)


threading.Thread(target=_tick_recorder_loop, daemon=True).start()


# ------------------------------------------------------ Alert Engine
# Background evaluation: every active alert is evaluated on
# its OWN stored timeframe, no matter which timeframe any chart pane shows -
# and even with no browser tab open at all.
# How it works, per alert, each cycle (3s, only when MT5 is live):
#   * price alerts: tick-level cross detection against the last seen bid.
#   * indicator alerts: the indicator's compute() runs on the alert's
#     timeframe bars (closed bars + the vault-built forming bar). Level
#     sweeps fire when a bar's range touches the level; event signals
#     (BUY/SELL) fire once their bar has closed. The first evaluation only
#     sets a baseline (last_eval_bar) so years of history can never replay.
#   * every fire goes through the same _fire_* helpers as the browser path,
#     so the sweep-key dedup + lifecycle (once/cooldown/expiry/anti-spam)
#     make double-fires impossible no matter which path sees the signal first.
_ALERT_ENGINE_S = 3.0        # engine cycle, seconds
_ALERT_ENGINE_MIN_RECOMP = 15  # min seconds between indicator recomputes
_ENGINE_STATS = {"cycles": 0, "evals": 0, "fires": 0, "errors": 0,
                 "last_cycle": 0, "last_error": ""}
_ENGINE_PRICE_LAST = {}      # symbol -> last bid seen by the engine
_ENGINE_LAST_COMP = {}       # alert_id -> last compute epoch


def _engine_eval_price(alert, now):
    """Tick-level cross detection for one price alert."""
    symbol = alert.get("symbol") or ""
    try:
        px = float(alert.get("price"))
    except (TypeError, ValueError):
        return
    q = None
    try:
        q = feed.get_quote(symbol)
    except Exception:
        return
    if not q or q.get("bid") is None:
        return
    bid = float(q["bid"])
    prev = _ENGINE_PRICE_LAST.get(symbol)
    _ENGINE_PRICE_LAST[symbol] = bid
    if prev is None or not px:
        return
    up = prev < px <= bid
    dn = prev > px >= bid
    if not (up or dn):
        return
    cond = alert.get("condition") or "crossing"
    if cond == "crossing up" and not up:
        return
    if cond == "crossing down" and not dn:
        return
    res = _fire_price_alert(alert, bid)
    if res.get("fired"):
        _ENGINE_STATS["fires"] += 1


def _engine_eval_indicator(alert, now):
    """Evaluate one indicator alert on its own timeframe.

    alerts with chart_mode "renko" are evaluated on Renko bricks
    (built exactly like the Renko chart) instead of candle bars.
    """
    tf = (alert.get("timeframe") or "").strip()
    if not tf:
        return  # legacy alert without a TF: browser path still covers it
    base_id = str(alert.get("indicator_id") or "").split("@")[0]
    if not base_id or indlib.get_indicator(base_id) is None:
        return
    symbol = (alert.get("symbol") or "").strip() or "XAUUSD"
    renko_mode = (alert.get("chart_mode") or "").strip() == "renko"
    try:
        box = float(alert.get("box") or 2.5)
    except (TypeError, ValueError):
        box = 2.5
    tf_sec = _TF_SEC.get(tf)
    if not renko_mode and not tf_sec:
        return
    aid = alert.get("id")

    try:
        if renko_mode:
            # the alert's timeframe selects the brick base series -
            # a 15s Renko alert evaluates on bricks built from 15-second
            # bars (live MT5 ticks), exactly like the Renko pane on 15s.
            base_tf = tf if tf in _TF_SEC else "1m"
            bricks = _indicator_renko_bricks(symbol, box, base_tf)
            bars = [b for b in bricks if not b.get("forming")]
            forming = bricks[-1] if bricks and bricks[-1].get("forming") else None
        else:
            bars = _indicator_chart_bars(symbol, tf)
    except Exception as e:
        raise RuntimeError("bars: %s" % e)
    if not bars:
        return
    if renko_mode:
        # A brick is closed the moment the next brick (or the forming brick)
        # exists after it - every non-forming brick in the list is closed.
        closed = bars
    else:
        # A bar that started more than one full period ago is definitely
        # closed - no wall-clock slot math, so D/W/M broker-day alignment
        # can't mislead us.
        closed = [b for b in bars if b["time"] < now - tf_sec]
        if not renko_mode:
            # Forming bar from our own tick vault lets level sweeps
            # be seen intrabar even with the browser closed. Event signals
            # still wait for bar close below (a forming-bar signal can vanish).
            forming = None
            try:
                slot = (int(now) // tf_sec) * tf_sec
                fb = vault.build_bars(symbol, slot, int(now), tf_sec)
                if fb and fb[-1]["time"] >= slot:
                    forming = fb[-1]
            except Exception:
                forming = None
    if not closed:
        return
    newest_closed = closed[-1]["time"]
    last_eval = int(alert.get("last_eval_bar") or 0)

    last_comp = _ENGINE_LAST_COMP.get(aid, 0)
    if newest_closed <= last_eval and now - last_comp < _ALERT_ENGINE_MIN_RECOMP:
        return  # nothing new; don't burn CPU recomputing
    _ENGINE_LAST_COMP[aid] = now

    result = _run_indicator(base_id, symbol, tf, alert.get("settings") or {},
                            mode=("renko" if renko_mode else "candles"),
                            box=box)
    sweeps = [s for s in (result.get("sweeps") or []) if s.get("alert")]
    uid = str(alert.get("indicator_id") or "")

    if last_eval <= 0:
        # First sight: baseline only - never replay history as alerts.
        alert_defs.update_alert(aid, {"last_eval_bar": newest_closed})
        return

    new_closed = [b for b in closed if b["time"] > last_eval]
    scan = list(new_closed)
    if forming is not None:
        scan.append(forming)

    eng_mode = "renko" if renko_mode else ""
    # pane-independent dedup key (base indicator id only) - the
    # browser fires with the same key, so "once per session breach per day"
    # holds across panes and across the browser/engine boundary. The full
    # pane-scoped uid is still used for the alert definition itself.
    base_uid = uid.split("@")[0]
    for sw in sweeps:
        try:
            price = float(sw.get("price"))
        except (TypeError, ValueError):
            continue
        key = "%s:%s" % (base_uid, sw.get("key"))
        if sw.get("event"):
            # Event signal: fire once its bar closed and is newer than the
            # last evaluated bar. (Signal time comes from the indicator;
            # ORB/renko all stamp "time" on event sweeps.)
            t = sw.get("time")
            try:
                t = int(t)
            except (TypeError, ValueError):
                continue
            if t > last_eval and t <= newest_closed:
                if alert_defs.sweep_already_fired(key):
                    continue
                res = _fire_indicator_alert(
                    alert, uid, alert.get("indicator_name") or base_id,
                    symbol, tf, sw.get("text") or "", price, key, True,
                    chart_mode=eng_mode, box=box, signal_time=t)
                if res.get("fired"):
                    _ENGINE_STATS["fires"] += 1
        else:
            # Level sweep: fire when any scanned bar's range touched the level.
            touched = any(b["low"] <= price <= b["high"] for b in scan)
            if touched and not alert_defs.sweep_already_fired(key):
                # Signal time = first scanned bar touching the level.
                touch_bar = next((b for b in scan
                                  if b["low"] <= price <= b["high"]), None)
                res = _fire_indicator_alert(
                    alert, uid, alert.get("indicator_name") or base_id,
                    symbol, tf, sw.get("text") or "", price, key, False,
                    chart_mode=eng_mode, box=box,
                    signal_time=(touch_bar["time"] if touch_bar else None))
                if res.get("fired"):
                    _ENGINE_STATS["fires"] += 1

    alert_defs.update_alert(aid, {"last_eval_bar": newest_closed})


def _alert_engine_loop():
    """Background thread: evaluate every active alert on its own timeframe."""
    while True:
        try:
            if feed.live:
                now = time.time()
                _ENGINE_STATS["cycles"] += 1
                _ENGINE_STATS["last_cycle"] = int(now)
                for alert in alert_defs.list_alerts():
                    try:
                        if alert.get("status") != "active":
                            continue
                        _ENGINE_STATS["evals"] += 1
                        if alert.get("kind") == "price":
                            _engine_eval_price(alert, now)
                        else:
                            _engine_eval_indicator(alert, now)
                    except Exception as e:
                        _ENGINE_STATS["errors"] += 1
                        _ENGINE_STATS["last_error"] = str(e)[:160]
        except Exception as e:
            _ENGINE_STATS["errors"] += 1
            _ENGINE_STATS["last_error"] = str(e)[:160]
        time.sleep(_ALERT_ENGINE_S)


threading.Thread(target=_alert_engine_loop, daemon=True).start()


def _prefetch_loop():
    """Background: once MT5 is live, backfill the smallest-available history
    (deep M1 + main TFs) for the default symbol into the disk cache, so TF
    switches are instant and reopening later only backfills the tail gap."""
    done_for = set()
    time.sleep(25)  # let the first user request win the MT5 pipe
    while True:
        try:
            if feed.live:
                sym = (feed.detect_symbol() or "XAUUSD").strip()
                if sym not in done_for:
                    for tf in ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "D"):
                        try:
                            _deep_history(sym, tf)
                        except Exception:
                            pass
                    done_for.add(sym)
        except Exception:
            pass
        time.sleep(120)


threading.Thread(target=_prefetch_loop, daemon=True).start()


# startup sync status - the frontend shows this while the chart
# prepares, so the user sees what the app is doing (MT5 connect -> history
# sync -> live) instead of a blank chart.
_STARTUP_STATUS = {
    "mt5_live": False,
    "warming": False,
    "warmed": False,
    "warm_message": "Starting...",
    "warmed_bars": 0,
}


def _startup_warm_history():
    """Warm the deep-history cache on server start so the first browser open
    is instant. Waits for MT5, then pulls the full 1m history once in the
    background; later browser opens just read the disk cache.

    every stage is reported to the ops bus so the startup overlay
    can narrate it (what / how long / what's stuck) instead of a static
    message."""
    try:
        ops.start("startup", "Server startup")
        # feed-aware boot narration ("OANDA", not "MT5", in OANDA mode).
        _fl = _FEED_NAME
        _hint = ("the terminal must be open and logged in" if _fl == "MT5"
                 else "the API token must be valid and OANDA reachable")
        ops.stage("startup", "Waiting for %s connection" % _fl, _hint)
        _STARTUP_STATUS["warm_message"] = "Waiting for %s connection..." % _fl
        for i in range(120):  # up to ~4 minutes
            if feed.live:  # cached 10s - notices a fresh connection promptly
                break
            if i % 5 == 4:
                ops.touch("startup",
                          "still waiting... %ds - %s"
                          % ((i + 1) * 2, _hint))
            time.sleep(2)
        _STARTUP_STATUS["mt5_live"] = bool(feed.live)
        if not feed.live:
            _STARTUP_STATUS["warm_message"] = (
                "%s not connected - charts will use demo/limited data. " % _fl +
                ("Start the MT5 terminal and restart the server."
                 if _fl == "MT5" else
                 "Check the API token in the Data dialog and restart."))
            ops.fail("startup",
                     "%s never connected after 4 minutes - %s"
                     % (_fl, _hint))
            return
        _STARTUP_STATUS["warming"] = True
        _STARTUP_STATUS["warm_message"] = "%s connected - syncing 1m history..." % _fl
        sym = (feed.detect_symbol() or "XAUUSD").strip()
        ops.stage("startup", "Syncing 1m history",
                  "%s - first pull can take a while on a slow feed" % sym)
        bars = _deep_history(sym, "1m")
        _STARTUP_STATUS["warming"] = False
        _STARTUP_STATUS["warmed"] = True
        _STARTUP_STATUS["warmed_bars"] = len(bars)
        # say it honestly when the warm served cache through the
        # circuit breaker instead of syncing live history.
        if getattr(feed, "hist_circuit", "closed") != "closed":
            _STARTUP_STATUS["warm_message"] = (
                "%s history unreachable - charts load from cache "
                "(%d 1m bars); retrying in background." % (_fl, len(bars)))
            ops.done("startup",
                     "%s history unreachable - serving %d cached bars, "
                     "retrying in background" % (_fl, len(bars)))
        else:
            _STARTUP_STATUS["warm_message"] = (
                "History synced: %d 1m bars. Chart is live." % len(bars))
            ops.done("startup", "History synced: %d 1m bars - chart is live"
                     % len(bars))
        print("[chart] startup warm done: %d bars for %s" % (len(bars), sym))
    except Exception as e:
        _STARTUP_STATUS["warming"] = False
        _STARTUP_STATUS["warm_message"] = "History sync failed: %s" % e
        ops.fail("startup", "History sync failed: %s" % e)
        print("[chart] startup warm failed: %s" % e)


threading.Thread(target=_startup_warm_history, daemon=True).start()


# max-depth history worker. One thread, symbols sync sequentially
# so MT5 is never hammered. ~20s after boot it queues the default gold
# symbol plus every watchlist symbol - each restart only fetches what's
# missing (tail gap + any older bars the broker has since exposed).
threading.Thread(target=_deep_sync_worker, daemon=True).start()


def _boot_deep_queue():
    try:
        time.sleep(20)
        syms = []
        try:
            syms.append(feed.detect_symbol())
        except Exception:
            pass
        try:
            for s in feed.get_watchlist() or []:
                syms.append(s)
        except Exception:
            pass
        seen = set()
        for s in syms:
            s = (s or "").strip()
            if s and s not in seen:
                seen.add(s)
                queue_deep_sync(s, "boot")
    except Exception as e:
        print("[chart] boot deep queue failed: %s" % e, flush=True)


threading.Thread(target=_boot_deep_queue, daemon=True).start()


@app.route("/api/startup_status")
def api_startup_status():
    """what the server is doing during startup (for the loading UI)."""
    try:
        now = int(time.time())
        day_start = (now // 86400) * 86400
        ticks_today = len(vault.fetch(
            (feed.detect_symbol() or "XAUUSD").strip(), day_start, now))
    except Exception:
        ticks_today = -1
    return jsonify({
        "mt5_live": bool(feed.live) or _STARTUP_STATUS["mt5_live"],
        # feed-aware boot labels ("OANDA [ok]" not "MT5 [ok]" in OANDA mode).
        "feed_label": _FEED_NAME,
        "warming": _STARTUP_STATUS["warming"],
        "warmed": _STARTUP_STATUS["warmed"],
        "message": _STARTUP_STATUS["warm_message"],
        "warmed_bars": _STARTUP_STATUS["warmed_bars"],
        "ticks_today": ticks_today,
        "server_time": int(time.time()),
        # circuit-breaker state drives the overlay's step list.
        "hist_circuit": getattr(feed, "hist_circuit", "closed"),
        # live startup progress (stage/elapsed/stuck/error).
        "startup_op": ops.snapshot("startup"),
        "tickgap_op": ops.snapshot("tickgap"),
    })


@app.route("/api/ops")
def api_ops():
    """live progress of every background operation - the UI
    narrates these instead of showing static 'Loading...' messages."""
    return jsonify({"ops": ops.all(), "server_time": int(time.time())})


@app.route("/api/ops/<op_id>")
def api_op(op_id):
    """single-op snapshot for focused polling (e.g. the watchlist
    sync dialog). Returns {"missing": True} when no such op exists."""
    snap = ops.snapshot(op_id)
    if snap is None:
        return jsonify({"missing": True, "id": op_id})
    return jsonify(snap)


# on startup, check the external DB for a tick gap (e.g. PC was
# off for 2 days). If missing trading days are found and MT5 is live, the
# gap is backfilled tick-by-tick automatically in the background.
def _startup_tick_gap_check():
    try:
        time.sleep(20)  # let MT5 connect first
        if not feed.live:
            return
        _tm = _imp("tick_manager")
        gap = _tm.tick_gap()
        if gap.get("gap_days"):
            print("[chart] tick gap detected: %d day(s) missing %s - "
                  "auto-backfilling..." % (gap["gap_days"],
                                         gap["missing_dates"]))
            _tm.start_gap_backfill(feed.detect_symbol(), feed)
        else:
            print("[chart] tick data up to date.")
    except Exception as e:
        print("[chart] tick gap check failed: %s" % e)


threading.Thread(target=_startup_tick_gap_check, daemon=True).start()


if __name__ == "__main__":
    # write our PID so stop_chart.bat can kill exactly this process.
    # PID file goes in %TEMP% (Windows) so it survives the user
    # deleting/replacing the app folder - the #1 cause of "old version"
    # still running after a fresh extract.
    try:
        if os.name == "nt":
            _piddir = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Windows\Temp"
        else:
            _piddir = "/tmp"
        _pidf = os.path.join(_piddir, "stocksophist_chart_server.pid")
        with open(_pidf, "w") as _pf:
            _pf.write(str(os.getpid()))
        import atexit as _ax
        _ax.register(lambda: os.path.exists(_pidf) and os.remove(_pidf))
    except Exception:
        pass
    # The trading dashboard also defaults to 5000. Set STOCKSOPHIST_CHART_PORT (e.g.
    # 5001) when both run on the same machine.
    CHART_PORT = int(os.environ.get("STOCKSOPHIST_CHART_PORT", "5000") or 5000)
    print(f"[chart] mode={'LIVE-MT5' if feed.live else 'DEMO'}  symbol={feed.detect_symbol()}")
    print(f"[chart] open http://localhost:{CHART_PORT}")
    # incremental files - start a fresh one for this run.
    # New bars accumulate here; merged into main DB on clean shutdown.
    # MT5 mode only - in OANDA mode the deep-sync worker owns
    # persistence and nothing ever appends to the incremental file.
    try:
        _bf = _imp("backfill")
        _sym = feed.detect_symbol()
        if _sym and _FEED_TAG_RAW != "oanda":
            _ipath = _bf.incr_start(_sym)
            print(f"[chart] incremental file: {_ipath}")
    except Exception as _e:
        print(f"[chart] incremental init skipped: {_e}")
    # Merge incrementals on clean exit (Ctrl+C, shutdown).
    import atexit
    def _merge_on_exit():
        try:
            _bf2 = _imp("backfill")
            _s = feed.detect_symbol()
            if _s:
                safe = "".join(c for c in _s if c.isalnum() or c in "_-")
                # merge target lives in the data dir, not the code tree.
                _mp = os.path.abspath(os.path.join(
                    DATA_DIR, f"{safe}_1m.parquet"))
                _n = _bf2.incr_merge_all(_s, _mp)
                if _n:
                    print(f"[chart] merged {_n} incremental bars into main DB")
        except Exception as _e2:
            print(f"[chart] merge on exit failed: {_e2}")
    atexit.register(_merge_on_exit)
    # Open the chart in the default browser - no more copy-pasting the address.
    # Set STOCKSOPHIST_NO_BROWSER=1 to skip.
    _bt = threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{CHART_PORT}"))
    _bt.daemon = True
    if os.environ.get("STOCKSOPHIST_NO_BROWSER") != "1":
        _bt.start()
    try:
        app.run(host="127.0.0.1", port=CHART_PORT, debug=False, threaded=True)
    except OSError:
        _bt.cancel()
        raise
