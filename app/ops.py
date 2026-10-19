"""background-operation progress bus.

Every long-running background job (startup history sync, Renko tick pull,
tick-gap backfill, watchlist backfill) reports here: which stage it is on,
an optional 0-100 percent, a human detail line, and - on failure - the real
error. The UI polls /api/ops (or reads the op embedded in a specific
endpoint's response) and narrates it, so a waiting user always sees WHAT
is happening, HOW FAR along it is, and WHAT it is stuck on - never just
"Loading...".

Thread-safe. Finished ops are kept for 5 minutes so a late poller still
sees the outcome, then pruned.
"""
import threading
import time

_OPS = {}
_LOCK = threading.Lock()
_DONE_TTL = 300  # seconds to keep a finished op visible
_STUCK_S = 30   # a running op idle longer than this is "stuck"


def _now():
    return time.time()


def _view(op, now):
    """JSON-safe view of one op record with live elapsed + stuck flag."""
    d = dict(op)
    d["elapsed"] = round(now - d["started_at"], 1)
    d["elapsed_s"] = d["elapsed"]  # alias: what the UI reads
    # Stuck = running but no heartbeat for a while (e.g. wedged inside one
    # blocking MT5 call). The UI turns this into a [!] warning, not a hang.
    d["stuck"] = bool(d["status"] == "running"
                      and now - d["updated_at"] > _STUCK_S)
    return d


def _pruned(op_id, now):
    op = _OPS.get(op_id)
    if (op and op["status"] != "running"
            and now - op["updated_at"] > _DONE_TTL):
        del _OPS[op_id]
        return True
    return False


def start(op_id, label):
    """(Re)start an operation. Any previous state for op_id is replaced."""
    with _LOCK:
        _OPS[op_id] = {
            "id": op_id,
            "label": label,
            "stage": "starting",
            "detail": "",
            "pct": None,
            "status": "running",   # running | done | error
            "error": "",
            "started_at": _now(),
            "updated_at": _now(),
        }


def stage(op_id, stage, detail="", pct=None):
    """Move a running op to a new stage. pct is 0-100 or None (unknown)."""
    with _LOCK:
        op = _OPS.get(op_id)
        if not op or op["status"] != "running":
            return
        op["stage"] = stage
        op["detail"] = detail
        op["pct"] = pct
        op["updated_at"] = _now()


def touch(op_id, detail=""):
    """Heartbeat: the op is alive (used while blocked inside one MT5 call)."""
    with _LOCK:
        op = _OPS.get(op_id)
        if not op or op["status"] != "running":
            return
        if detail:
            op["detail"] = detail
        op["updated_at"] = _now()


def done(op_id, detail="", result=None):
    """Mark an op finished. result is an optional JSON-safe payload the UI
    picks up when the op completes (e.g. the watchlist sync's symbol list).
    """
    with _LOCK:
        op = _OPS.get(op_id)
        if not op:
            return
        op["status"] = "done"
        if detail:
            op["detail"] = detail
        if result is not None:
            op["result"] = result
        op["updated_at"] = _now()


def fail(op_id, error):
    """Record the REAL error - the UI shows it verbatim, no masking."""
    with _LOCK:
        op = _OPS.get(op_id)
        if not op:
            return
        op["status"] = "error"
        op["error"] = str(error)
        op["updated_at"] = _now()


def snapshot(op_id):
    """One op as a JSON-safe dict (with live elapsed + stuck flag), or None.
    Finished ops older than _DONE_TTL are pruned on read."""
    now = _now()
    with _LOCK:
        if _pruned(op_id, now):
            return None
        op = _OPS.get(op_id)
        if not op:
            return None
        d = dict(op)
    return _view(d, now)


def all():
    """All ops (running first), pruning finished ones older than _DONE_TTL."""
    now = _now()
    with _LOCK:
        stale = [k for k, op in _OPS.items()
                 if op["status"] != "running"
                 and now - op["updated_at"] > _DONE_TTL]
        for k in stale:
            del _OPS[k]
        ops = [dict(o) for o in _OPS.values()]
    ops = [_view(o, now) for o in ops]
    ops.sort(key=lambda o: (o["status"] != "running", o["started_at"]))
    return ops


def reset(op_id):
    with _LOCK:
        _OPS.pop(op_id, None)


class DeadlineExceeded(RuntimeError):
    """run_deadline gave up waiting - the worker may still be wedged inside
    a blocking call (a hung C-level MT5 call cannot be killed from here),
    so it keeps running as a daemon while the caller gets an honest error."""


def run_deadline(fn, timeout_s, what):
    """Run fn() in a daemon worker; return its value, or raise
    DeadlineExceeded after timeout_s seconds. fn's own exceptions propagate
    unchanged. the indicator endpoint's overall deadline."""
    out = {}

    def _work():
        try:
            out["res"] = fn()
        except Exception as e:  # noqa: BLE001 - propagated to caller
            out["err"] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise DeadlineExceeded(
            "%s timed out after %ss - MT5 is not responding "
            "(terminal may still be syncing)" % (what, timeout_s))
    if "err" in out:
        raise out["err"]
    return out["res"]
