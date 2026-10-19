#!/usr/bin/env python3
"""StockSophist Tick Manager.

Tick-by-tick data lifecycle for the standalone history database:

1. GAP DETECTION - compare the DB's newest tick vs now; report missing days.
2. GAP BACKFILL - fetch missing ticks day-by-day from MT5 into the main DB.
3. INCREMENTAL BACKUP - fetch a date range from MT5 into a SEPARATE .db
   file (stocksophist_ticks_incr_YYYYMMDD_YYYYMMDD.db) in the data folder.
4. MERGE INCREMENTALS - merge one or more incremental .db files' ticks
   into the main database (dedupe by timestamp), ending with one file.
5. PARQUET IMPORT - import tick parquet files (from fetch_ticks.py) into
   the main database.

The main database lives in the EXTERNAL data folder (see history_db.py)
and is never bundled with the app. This module only reads/writes .db
files there - never inside the app tree.
"""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

from history_db import find_database, get_db, refresh as _db_refresh
try:
    import ops  # progress bus (optional - tick_manager also runs standalone)
except ImportError:
    ops = None

_INCR_PREFIX = "stocksophist_ticks_incr_"
_JOBS = {}
_JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _data_dir():
    """Directory holding the main DB (its parent)."""
    p = find_database()
    if p:
        return os.path.dirname(p)
    if os.name == "nt":
        return r"C:\stocksophist\data"
    env = os.environ.get("STOCKSOPHIST_DATA_DIR", "").strip()
    if env and os.path.isdir(os.path.expandvars(env)):
        return os.path.expandvars(env)
    return os.path.join(os.path.expanduser("~"), "stocksophist_data")


def _connect(path, readonly=False):
    uri = "file:%s?mode=%s" % (path, "ro" if readonly else "rwc")
    return sqlite3.connect(uri, uri=True, timeout=60)


def _ensure_ticks_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS ticks "
                "(time INTEGER PRIMARY KEY, bid REAL, ask REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT)")


def _job_new(kind):
    jid = "%s_%d" % (kind, int(time.time() * 1000))
    with _JOBS_LOCK:
        _JOBS[jid] = {"job_id": jid, "kind": kind, "done": False,
                      "message": "starting...", "progress": 0,
                      "updated_at": time.time()}
    _ensure_watchdog()
    return jid


def _job_update(jid, **kw):
    with _JOBS_LOCK:
        if jid in _JOBS:
            _JOBS[jid].update(kw)
            _JOBS[jid]["updated_at"] = time.time()


def _job_watchdog():
    """mark jobs with no heartbeat in 3 minutes as failed, so the
    UI can never sit on 'working...' forever."""
    while True:
        time.sleep(30)
        try:
            now = time.time()
            with _JOBS_LOCK:
                for jid, j in list(_JOBS.items()):
                    if not j.get("done") and now - j.get("updated_at", now) > 180:
                        j["done"] = True
                        j["message"] = ("Job timed out (no progress for 3 min) - "
                                        "check the server console, then retry.")
        except Exception:
            pass


_WATCHDOG_STARTED = False


def _ensure_watchdog():
    global _WATCHDOG_STARTED
    if not _WATCHDOG_STARTED:
        _WATCHDOG_STARTED = True
        t = threading.Thread(target=_job_watchdog, daemon=True)
        t.start()


def job_status(jid):
    with _JOBS_LOCK:
        return dict(_JOBS.get(jid, {"done": True,
                                   "message": "unknown job"}))


# --------------------------------------------------------------------------
# 1. Gap detection
# --------------------------------------------------------------------------

def tick_gap():
    """How much tick data is missing.

    Returns {db_newest, now, gap_days, missing_dates[]}.
    Weekends are excluded - only trading days count as missing.
    """
    db = get_db()
    st = db.status()
    now = datetime.now(timezone.utc)
    out = {"available": st["available"], "db_newest": st["tick_end"],
           "now": now.strftime("%Y-%m-%d"),
           "gap_days": 0, "missing_dates": []}
    if not st["available"] or not st["tick_end"]:
        return out
    newest = datetime.strptime(st["tick_end"], "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    d = newest + timedelta(days=1)
    missing = []
    while d.date() < now.date():
        if d.weekday() < 5:
            missing.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    # Today counts if it's a weekday and past 02:00 UTC (ticks flowing).
    if now.weekday() < 5 and now.hour >= 2 and \
            newest.date() < now.date():
        today = now.strftime("%Y-%m-%d")
        if today not in missing:
            missing.append(today)
    out["gap_days"] = len(missing)
    out["missing_dates"] = missing
    return out


# --------------------------------------------------------------------------
# 2/3. Fetch ticks from MT5 (shared by backfill + incremental backup)
# --------------------------------------------------------------------------

def _fetch_day_ticks(feed, symbol, day):
    """All ticks for one UTC day. Returns list of (time_ms, bid, ask)."""
    import MetaTrader5 as mt5
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) == 0:
        return []
    out = []
    seen = set()
    for t in ticks:
        ms = int(t["time"]) * 1000
        if ms in seen:
            continue
        seen.add(ms)
        out.append((ms, float(t["bid"]), float(t["ask"])))
    return out


def _resolve_symbol(feed, symbol):
    sym, _note = feed.resolve_symbol(symbol)
    return sym


# --------------------------------------------------------------------------
# 2. Gap backfill (into the main DB)
# --------------------------------------------------------------------------

def start_gap_backfill(symbol, feed):
    """Background job: fill missing tick days into the main DB."""
    jid = _job_new("gap_backfill")

    def run():
        try:
            if ops: ops.start("tickgap", "Tick history gap backfill")
            db_path = find_database()
            if not db_path:
                _job_update(jid, done=True, progress=100,
                            message="Main database not found - place the .db file "
                                    "in C:\\stocksophist\\data first, then click (refresh) Rescan.")
                if ops: ops.fail("tickgap", "main database not found")
                return
            gap = tick_gap()
            missing = gap["missing_dates"]
            if not missing:
                _job_update(jid, done=True, progress=100,
                            message="No tick gap - database is up to date "
                                    "(newest: %s)." % gap.get("db_newest"))
                if ops: ops.done("tickgap", "tick data up to date")
                return
            sym = _resolve_symbol(feed, symbol)
            mt5_live = bool(getattr(feed, "live", False))
            if not mt5_live:
                _job_update(jid, done=True, progress=100,
                            message="MT5 is not connected - start the MT5 terminal "
                                    "and wait for login, then retry.")
                if ops: ops.fail("tickgap", "MT5 not connected")
                return
            total = 0
            _job_update(jid, progress=0,
                        message="MT5 connected (%s). Fetching %d missing day(s) "
                                "tick-by-tick..." % (sym, len(missing)))
            if ops: ops.stage("tickgap", "Fetching missing days",
                              "%d day(s) missing" % len(missing), pct=0)
            for i, ds in enumerate(missing):
                day = datetime.strptime(ds, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
                ticks = _fetch_day_ticks(feed, sym, day)
                if ticks:
                    con = _connect(db_path)
                    try:
                        _ensure_ticks_table(con)
                        con.executemany(
                            "INSERT OR IGNORE INTO ticks VALUES (?,?,?)",
                            ticks)
                        con.execute(
                            "INSERT OR REPLACE INTO meta VALUES "
                            "('ticks_updated_utc', ?)",
                            (datetime.now(timezone.utc).isoformat(),))
                        con.commit()
                    finally:
                        con.close()
                    total += len(ticks)
                _job_update(jid, progress=int((i + 1) / len(missing) * 100),
                            message="Backfilled %s (%d/%d days, %d ticks)..."
                            % (ds, i + 1, len(missing), total))
                if ops: ops.stage("tickgap", "Backfilling %s" % ds,
                                  "%d/%d days, %d ticks so far"
                                  % (i + 1, len(missing), total),
                                  pct=int((i + 1) / len(missing) * 100))
            try:
                con = _connect(db_path)
                n = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
                con.execute("INSERT OR REPLACE INTO meta VALUES "
                            "('ticks_count', ?)", (str(n),))
                con.commit()
                con.close()
            except Exception:
                pass
            _db_refresh()
            _job_update(jid, done=True, progress=100,
                        message="Gap backfill done: %d days, %d ticks." %
                        (len(missing), total))
            if ops: ops.done("tickgap", "done: %d days, %d ticks"
                             % (len(missing), total))
        except Exception as e:
            _job_update(jid, done=True, message="Backfill failed: %s" % e)
            if ops: ops.fail("tickgap", "Backfill failed: %s" % e)

    threading.Thread(target=run, daemon=True).start()
    return jid


# --------------------------------------------------------------------------
# 3. Incremental backup (separate .db file)
# --------------------------------------------------------------------------

def start_incr_backup(symbol, feed, start_date, end_date):
    """Background job: fetch ticks for [start_date, end_date] into a
    separate stocksophist_ticks_incr_YYYYMMDD_YYYYMMDD.db in the data folder."""
    jid = _job_new("incr_backup")

    def run():
        try:
            sym = _resolve_symbol(feed, symbol)
            s = datetime.strptime(start_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            e = datetime.strptime(end_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            days = []
            d = s
            while d <= e:
                if d.weekday() < 5:
                    days.append(d)
                d += timedelta(days=1)
            if not days:
                _job_update(jid, done=True,
                            message="No trading days in range.")
                return
            fname = "%s%s_%s.db" % (_INCR_PREFIX,
                                    days[0].strftime("%Y%m%d"),
                                    days[-1].strftime("%Y%m%d"))
            out_path = os.path.join(_data_dir(), fname)
            os.makedirs(_data_dir(), exist_ok=True)
            if os.path.exists(out_path):
                os.remove(out_path)
            con = _connect(out_path)
            _ensure_ticks_table(con)
            total = 0
            for i, day in enumerate(days):
                ticks = _fetch_day_ticks(feed, sym, day)
                if ticks:
                    con.executemany(
                        "INSERT OR IGNORE INTO ticks VALUES (?,?,?)", ticks)
                    con.commit()
                    total += len(ticks)
                _job_update(jid, progress=int((i + 1) / len(days) * 100),
                            message="Fetching %s (%d/%d)..." %
                            (day.strftime("%Y-%m-%d"), i + 1, len(days)))
            con.execute("CREATE INDEX IF NOT EXISTS idx_ticks_time "
                        "ON ticks(time)")
            for k, v in [
                ("symbol", "XAUUSD"), ("timeframe", "ticks"),
                ("created_utc", datetime.now(timezone.utc).isoformat()),
                ("source", "XM via MT5 (incremental backup)"),
                ("range_start", days[0].strftime("%Y-%m-%d")),
                ("range_end", days[-1].strftime("%Y-%m-%d")),
                ("ticks_count", str(total)),
            ]:
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (k, v))
            con.commit()
            con.close()
            _job_update(jid, done=True, progress=100,
                        message="Incremental backup done: %s (%d ticks)." %
                        (fname, total), file=fname)
        except Exception as e:
            _job_update(jid, done=True, message="Backup failed: %s" % e)

    threading.Thread(target=run, daemon=True).start()
    return jid


def list_incr_backups():
    """Incremental .db files waiting in the data folder."""
    d = _data_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        if f.startswith(_INCR_PREFIX) and f.endswith(".db"):
            p = os.path.join(d, f)
            info = {"file": f, "size_mb": round(os.path.getsize(p) / 1e6, 1)}
            try:
                con = _connect(p, readonly=True)
                r = con.execute("SELECT COUNT(*) FROM ticks").fetchone()
                info["ticks"] = r[0] if r else 0
                meta = dict(con.execute("SELECT key, value FROM meta"))
                info["range"] = "%s -> %s" % (
                    meta.get("range_start", "?"), meta.get("range_end", "?"))
                con.close()
            except Exception:
                info["ticks"] = 0
                info["range"] = "?"
            out.append(info)
    return out


# --------------------------------------------------------------------------
# 4. Merge incremental DBs into the main DB
# --------------------------------------------------------------------------

def start_merge_incr(files):
    """Background job: merge ticks from incremental .db files into the
    main database. Handles one or many files. Deletes incrementals after
    a successful merge."""
    jid = _job_new("merge_incr")

    def run():
        try:
            main = find_database()
            if not main:
                _job_update(jid, done=True,
                            message="Main database not found.")
                return
            d = _data_dir()
            # Safety: never merge the main DB into itself.
            main_base = os.path.basename(main)
            targets = [f for f in files
                       if f != main_base and f.startswith(_INCR_PREFIX)
                       and f.endswith(".db")]
            if not targets:
                _job_update(jid, done=True,
                            message="No incremental files to merge.")
                return
            con = _connect(main)
            _ensure_ticks_table(con)
            total_new = 0
            for i, f in enumerate(targets):
                p = os.path.join(d, f)
                if not os.path.exists(p):
                    continue
                _job_update(jid, progress=int(i / len(targets) * 90),
                            message="Merging %s (%d/%d)..." %
                            (f, i + 1, len(targets)))
                src = _connect(p, readonly=True)
                try:
                    # Stream in chunks to bound memory.
                    cur = src.execute(
                        "SELECT time, bid, ask FROM ticks ORDER BY time")
                    batch = []
                    for row in cur:
                        batch.append(row)
                        if len(batch) >= 50000:
                            cur2 = con.executemany(
                                "INSERT OR IGNORE INTO ticks VALUES (?,?,?)",
                                batch)
                            total_new += cur2.rowcount
                            batch = []
                    if batch:
                        cur2 = con.executemany(
                            "INSERT OR IGNORE INTO ticks VALUES (?,?,?)",
                            batch)
                        total_new += cur2.rowcount
                    con.commit()
                finally:
                    src.close()
            # Refresh meta + index.
            con.execute("CREATE INDEX IF NOT EXISTS idx_ticks_time "
                        "ON ticks(time)")
            n = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
            con.execute("INSERT OR REPLACE INTO meta VALUES "
                        "('ticks_count', ?)", (str(n),))
            con.execute("INSERT OR REPLACE INTO meta VALUES "
                        "('ticks_updated_utc', ?)",
                        (datetime.now(timezone.utc).isoformat(),))
            con.commit()
            con.close()
            # Delete merged incrementals - one file remains.
            for f in targets:
                try:
                    os.remove(os.path.join(d, f))
                except Exception:
                    pass
            _db_refresh()
            _job_update(jid, done=True, progress=100,
                        message="Merged %d file(s): %d new ticks, "
                        "%d total. Incrementals cleaned up." %
                        (len(targets), total_new, n))
        except Exception as e:
            _job_update(jid, done=True, message="Merge failed: %s" % e)

    threading.Thread(target=run, daemon=True).start()
    return jid


# --------------------------------------------------------------------------
# 5. Parquet import (from fetch_ticks.py output)
# --------------------------------------------------------------------------

def import_tick_parquet(parquet_path):
    """One-shot import of a tick parquet file into the main DB ticks table.
    Returns (ticks_added, message)."""
    import pandas as pd
    main = find_database()
    if not main:
        return 0, "Main database not found."
    df = pd.read_parquet(parquet_path)
    if "time" not in df.columns or "bid" not in df.columns:
        return 0, "Parquet must have time/bid/ask columns."
    t = df["time"].iloc[0]
    # MT5 seconds vs ms auto-detect.
    mult = 1000 if t < 1e11 else 1
    df = df[["time", "bid", "ask"]].copy()
    df["time"] = (df["time"] * mult).astype("int64")
    df = df.drop_duplicates("time").sort_values("time")
    rows = [(int(r.time), float(r.bid), float(r.ask))
            for r in df.itertuples()]
    con = _connect(main)
    _ensure_ticks_table(con)
    cur = con.executemany("INSERT OR IGNORE INTO ticks VALUES (?,?,?)", rows)
    added = cur.rowcount
    n = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    con.execute("INSERT OR REPLACE INTO meta VALUES ('ticks_count', ?)",
                (str(n),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('ticks_updated_utc', ?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    con.close()
    _db_refresh()
    return added, "Imported %d new ticks (%d total)." % (added, n)
