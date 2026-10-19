"""StockSophist Charts - Manual backfill, backup & restore.

Features:
  1. A backfill button: pull history from MT5 on demand.
     - M1 bars as deep as the broker serves (1-2+ years on the broker).
     - Tick-by-tick for the last N days (broker retention, ~15-30 days).
     - Higher TFs resample from M1 (exact - no separate fetch needed).
  2. Backup: all chart data -> single timestamped file.
  3. Restore: load the file, gap-fill missing ranges from MT5, merge.

Incremental files + 10-year history import.
  - App start: creates incr/m1_SYMBOL_YYYYMMDD_HHMMSS.parquet (empty).
  - During run: new bars accumulate in the incremental file (small, fast).
  - App close: incremental merges into main DB, file deleted.
  - Next start: fresh incremental file.
  - Gap on start (PC was off): MT5 fills the gap -> incremental -> merge on close.
  - Import: CSV/TXT 1m history (10yr files) -> normalize -> merge into main.

MT5 limitations (honest):
  - Ticks older than broker retention (~15-30d) are purged; unrecoverable.
  - M1 depth varies by broker + terminal "Max bars in chart" setting.
  - Weekend/holiday gaps are normal.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_BACKUP_DIR = None

# runtime-dir override. server.py calls set_runtime_dir(DATA_DIR)
# once at startup so every runtime-written backfill location lives OUTSIDE
# the code tree (delete-and-replace updates never wipe user data, and the
# user can back the data dir up separately). When never called (standalone
# / script use) the helpers fall back to the legacy in-tree locations.
_RUNTIME_DIR = None


def set_runtime_dir(path):
    """Point all backfill runtime dirs/files at `path` (the app DATA_DIR)."""
    global _RUNTIME_DIR
    _RUNTIME_DIR = os.path.abspath(path)
    for sub in ("incr", "ticks", "backups"):
        os.makedirs(os.path.join(_RUNTIME_DIR, sub), exist_ok=True)


def _rt_base():
    """Base dir for runtime files: DATA_DIR when set, else legacy in-tree."""
    if _RUNTIME_DIR:
        return _RUNTIME_DIR
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _rt_cache_base():
    """Legacy in-tree cache dir (used only when set_runtime_dir wasn't called)."""
    return os.path.join(_rt_base(), "cache")


def _backup_dir():
    global _BACKUP_DIR
    if _BACKUP_DIR is None:
        # DATA_DIR/backups when the runtime dir is set.
        _BACKUP_DIR = os.path.join(_rt_base(), "backups")
        os.makedirs(_BACKUP_DIR, exist_ok=True)
    return _BACKUP_DIR


def _job_new(kind):
    jid = uuid.uuid4().hex[:8]
    with _JOBS_LOCK:
        _JOBS[jid] = {"kind": kind, "status": "running", "progress": 0,
                      "message": "Starting...", "started": time.time(),
                      "done": False, "error": None}
    return jid


def _job_update(jid, **kw):
    with _JOBS_LOCK:
        if jid in _JOBS:
            _JOBS[jid].update(kw)


def job_status(jid):
    with _JOBS_LOCK:
        return dict(_JOBS.get(jid, {"status": "unknown"}))


def _tick_dir():
    # DATA_DIR/ticks when the runtime dir is set.
    d = os.path.join(_rt_base() if _RUNTIME_DIR else _rt_cache_base(), "ticks")
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


def _tick_path(symbol):
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    return os.path.join(_tick_dir(), f"ticks_{safe}.parquet")


def start_backfill(symbol, feed, tick_days=15):
    """Deep backfill in a background thread. Returns job_id."""
    jid = _job_new("backfill")

    def _run():
        try:
            _job_update(jid, progress=5,
                        message="Fetching M1 history from MT5...")
            df_m1 = feed.get_m1_deep(symbol, target=1_000_000)
            n_m1 = len(df_m1)
            _job_update(jid, progress=60,
                        message=f"M1 done: {n_m1:,} bars. Fetching ticks...")
            n_ticks = 0
            try:
                n_ticks = _backfill_ticks(symbol, tick_days, jid)
            except Exception as e:
                _job_update(jid, message=f"M1 done ({n_m1:,} bars). "
                                        f"Ticks skipped: {e}")
            _job_update(jid, progress=100, status="done", done=True,
                        message=f"Done: {n_m1:,} M1 bars, {n_ticks:,} ticks.")
        except Exception as e:
            _job_update(jid, status="error", done=True, error=str(e),
                        message=f"Backfill failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jid


def _backfill_ticks(symbol, days, jid):
    """Pull tick-by-tick for the last `days` days -> local parquet."""
    import MetaTrader5 as mt5
    from datetime import timedelta
    frames = []
    total = 0
    now = datetime.now(timezone.utc)
    for d in range(days):
        day_end = now - timedelta(days=d)
        day_start = day_end - timedelta(days=1)
        ticks = mt5.copy_ticks_range(
            symbol, day_start, day_end, mt5.COPY_TICKS_ALL)
        if ticks is not None and len(ticks):
            df = pd.DataFrame(ticks)
            frames.append(df)
            total += len(df)
        _job_update(jid, progress=60 + int(35 * (d + 1) / max(days, 1)),
                    message=f"Ticks: day {d + 1}/{days} ({total:,})...")
    if frames:
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates("time").sort_values("time")
        # Merge with any existing tick cache (backup wins nothing; union).
        p = _tick_path(symbol)
        if os.path.exists(p):
            old = pd.read_parquet(p)
            out = pd.concat([old, out]).drop_duplicates("time").sort_values(
                "time").reset_index(drop=True)
        out.to_parquet(p, index=False)
    return total


def create_backup(symbol, feed):
    """All cached history for `symbol` -> single timestamped .db file."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    path = os.path.join(_backup_dir(), f"stocksophist_backup_{safe}_{ts}.db")
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES (?, ?)", ("symbol", symbol))
        con.execute("INSERT INTO meta VALUES (?, ?)", ("created_utc",
                    datetime.now(timezone.utc).isoformat()))
        con.execute("INSERT INTO meta VALUES (?, ?)", ("version", "3.3.17"))
        df_m1 = feed.get_m1_deep(symbol, target=1_000_000)
        df_m1[["time", "open", "high", "low", "close"]].to_sql(
            "m1", con, index=False, if_exists="replace")
        con.execute("CREATE INDEX IF NOT EXISTS idx_m1_time ON m1(time)")
        tp = _tick_path(symbol)
        if os.path.exists(tp):
            pd.read_parquet(tp).to_sql("ticks", con, index=False,
                                       if_exists="replace")
        con.commit()
    finally:
        con.close()
    return path


def restore_backup(path, symbol, feed):
    """Load backup .db, gap-fill missing ranges from MT5, merge. Returns job."""
    jid = _job_new("restore")

    def _run():
        try:
            _job_update(jid, progress=10, message="Reading backup...")
            con = sqlite3.connect(path)
            try:
                df_m1 = pd.read_sql("SELECT * FROM m1 ORDER BY time", con)
                try:
                    df_ticks = pd.read_sql("SELECT * FROM ticks ORDER BY time",
                                           con)
                except Exception:
                    df_ticks = None
            finally:
                con.close()
            restored = len(df_m1)
            # Store restored M1 via the feed's cache path.
            _store_m1_cache(feed, symbol, df_m1)
            if df_ticks is not None and len(df_ticks):
                tp = _tick_path(symbol)
                if os.path.exists(tp):
                    old = pd.read_parquet(tp)
                    df_ticks = pd.concat([old, df_ticks]).drop_duplicates(
                        "time").sort_values("time").reset_index(drop=True)
                df_ticks.to_parquet(tp, index=False)
            _job_update(jid, progress=50,
                        message=f"Restored {restored:,} bars. "
                                f"Checking MT5 for gaps...")
            live = feed.get_m1_deep(symbol, target=200_000)
            have = set(df_m1["time"].astype("int64"))
            missing = live[~live["time"].astype("int64").isin(have)]
            gapfilled = len(missing)
            if gapfilled:
                merged = pd.concat([df_m1, missing]).drop_duplicates(
                    "time").sort_values("time").reset_index(drop=True)
                _store_m1_cache(feed, symbol, merged)
            _job_update(jid, progress=100, status="done", done=True,
                        message=f"Restore done: {restored:,} from backup, "
                                f"{gapfilled:,} new from MT5.")
        except Exception as e:
            _job_update(jid, status="error", done=True, error=str(e),
                        message=f"Restore failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jid


def _store_m1_cache(feed, symbol, df_m1):
    """Write an M1 dataframe through the feed's own disk cache."""
    # Feed caches deep M1 via _deep_cache_path; reuse it if present.
    fn = getattr(feed, "_deep_cache_path", None)
    if callable(fn):
        try:
            p = fn(symbol, "1m")
            df_m1[["time", "open", "high", "low", "close"]].to_parquet(
                p, index=False)
            return
        except Exception:
            pass
    # Fallback: parquet next to the tick cache.
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    df_m1[["time", "open", "high", "low", "close"]].to_parquet(
        os.path.join(_tick_dir(), f"m1_{safe}.parquet"), index=False)


def list_backups():
    d = _backup_dir()
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        if f.startswith("stocksophist_backup_") and f.endswith(".db"):
            p = os.path.join(d, f)
            out.append({"file": f, "path": p,
                        "size_mb": round(os.path.getsize(p) / 1e6, 1),
                        "mtime": datetime.fromtimestamp(
                            os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    return out


# --------------------------------------------------------------------------
# Incremental files - per-session small files, merged on clean exit.
# --------------------------------------------------------------------------

def _incr_dir():
    # DATA_DIR/incr when the runtime dir is set.
    d = os.path.join(_rt_base() if _RUNTIME_DIR else _rt_cache_base(), "incr")
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


# Active incremental file for this run: (symbol -> path).
_ACTIVE_INCR = {}


def incr_start(symbol):
    """Create a fresh incremental file for this app run. Returns path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    path = os.path.join(_incr_dir(), f"m1_{safe}_incr_{ts}.parquet")
    # Empty with correct schema.
    pd.DataFrame(columns=["time", "open", "high", "low", "close"]).to_parquet(
        path, index=False)
    _ACTIVE_INCR[symbol] = path
    return path


def incr_append(symbol, df_bars):
    """Append new bars to this run's incremental file."""
    path = _ACTIVE_INCR.get(symbol)
    if not path:
        path = incr_start(symbol)
    cols = ["time", "open", "high", "low", "close"]
    new = df_bars[cols].copy()
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            old = pd.read_parquet(path)
            new = pd.concat([old, new])
        except Exception:
            pass
    new = new.drop_duplicates("time").sort_values("time").reset_index(
        drop=True)
    new.to_parquet(path, index=False)
    return len(new)


def incr_merge_all(symbol, main_path):
    """Merge all incremental files for `symbol` into the main DB.

    Called on clean app shutdown. Each incremental is merged (dedup by
    time), then deleted. Returns total bars merged.
    """
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    d = _incr_dir()
    pattern = f"m1_{safe}_incr_"
    merged = 0
    frames = []
    for f in sorted(os.listdir(d)):
        if f.startswith(pattern) and f.endswith(".parquet"):
            p = os.path.join(d, f)
            try:
                df = pd.read_parquet(p)
                if len(df):
                    frames.append(df)
                    merged += len(df)
                os.remove(p)
            except Exception:
                pass
    if frames:
        incr_df = pd.concat(frames).drop_duplicates("time").sort_values(
            "time").reset_index(drop=True)
        if os.path.exists(main_path):
            main_df = pd.read_parquet(main_path)
            combined = pd.concat([main_df, incr_df]).drop_duplicates(
                "time").sort_values("time").reset_index(drop=True)
        else:
            combined = incr_df
        combined[["time", "open", "high", "low", "close"]].to_parquet(
            main_path, index=False)
    _ACTIVE_INCR.pop(symbol, None)
    return merged


def incr_list(symbol):
    """List pending incremental files (not yet merged)."""
    safe = "".join(c for c in symbol if c.isalnum() or c in "_-")
    d = _incr_dir()
    out = []
    for f in sorted(os.listdir(d)):
        if f.startswith(f"m1_{safe}_incr_") and f.endswith(".parquet"):
            p = os.path.join(d, f)
            try:
                n = len(pd.read_parquet(p))
            except Exception:
                n = 0
            out.append({"file": f, "bars": n,
                        "size_kb": round(os.path.getsize(p) / 1024, 1)})
    return out


# --------------------------------------------------------------------------
# History import - CSV/TXT 1m files (e.g. 10-year history).
# --------------------------------------------------------------------------

def import_history_csv(path, symbol, tz="UTC"):
    """Import a CSV/TXT 1m history file into the main database.

    Handles two formats:
    1. With headers: columns datetime/timestamp/time, open, high, low, close
       (case-insensitive, auto-delimiter).
    2. Dukascopy ASCII (no headers): YYYYMMDD HHMMSS;O;H;L;C;V
       (semicolon-delimited, UTC).
    `tz`: timezone of the file's timestamps ("UTC", "Asia/Kolkata",
    "Europe/London", etc.). Converted to UTC epoch seconds for storage.
    The chart displays in IST; sessions use IST. Storage is UTC (standard).
    Symbol is normalized: XAUUSD/GOLD/GOLD# all -> XAUUSD (one database).
    """
    # Normalize symbol: smart, not blind.
    canon = normalize_symbol(symbol)
    # Auto-detect from filename if symbol not given.
    if not symbol or symbol.upper() == "XAUUSD":
        detected = symbol_from_filename(path)
        if detected != "XAUUSD" or "XAUUSD" in os.path.basename(path).upper() or "GOLD" in os.path.basename(path).upper():
            canon = detected
    jid = _job_new("import")

    def _run():
        try:
            _job_update(jid, progress=10, message="Reading file...")
            # Auto-detect delimiter (comma, tab, semicolon).
            with open(path, "r") as f:
                sample = f.read(8192)
            delim = ","
            for cand in ["\t", ";", "|"]:
                if sample.count(cand) > sample.count(delim):
                    delim = cand
            # Detect Dukascopy format: starts with YYYYMMDD (no headers).
            first_line = sample.strip().split("\n")[0]
            is_dukascopy = bool(_re.match(r"^\d{8}\s+\d{6}\s*;", first_line))
            # MT4/MT5 export format: YYYY.MM.DD,HH:MM,O,H,L,C,V (no headers)
            is_mt = bool(_re.match(r"^\d{4}\.\d{2}\.\d{2},\d{2}:\d{2},", first_line))
            if is_dukascopy:
                # Dukascopy ASCII: YYYYMMDD HHMMSS;O;H;L;C;V (UTC, no headers)
                df = pd.read_csv(path, sep=";", header=None,
                                 names=["dt", "o", "h", "l", "c", "v"])
                _job_update(jid, progress=30,
                            message=f"Dukascopy format detected. "
                                    f"Parsing {len(df):,} rows...")
                ts = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S", utc=True)
                out = pd.DataFrame({
                    "time": (ts.astype("int64") // 10**9).astype("int64"),
                    "open": pd.to_numeric(df["o"], errors="coerce"),
                    "high": pd.to_numeric(df["h"], errors="coerce"),
                    "low": pd.to_numeric(df["l"], errors="coerce"),
                    "close": pd.to_numeric(df["c"], errors="coerce"),
                })
            elif is_mt:
                # MetaTrader: YYYY.MM.DD,HH:MM,O,H,L,C,V (UTC, no headers)
                df = pd.read_csv(path, header=None,
                                 names=["date", "hm", "o", "h", "l", "c", "v"])
                _job_update(jid, progress=30,
                            message=f"MetaTrader format detected. "
                                    f"Parsing {len(df):,} rows...")
                ts = pd.to_datetime(df["date"] + " " + df["hm"],
                                    format="%Y.%m.%d %H:%M", utc=True)
                out = pd.DataFrame({
                    "time": (ts.astype("int64") // 10**9).astype("int64"),
                    "open": pd.to_numeric(df["o"], errors="coerce"),
                    "high": pd.to_numeric(df["h"], errors="coerce"),
                    "low": pd.to_numeric(df["l"], errors="coerce"),
                    "close": pd.to_numeric(df["c"], errors="coerce"),
                })
            else:
                df = pd.read_csv(path, sep=delim)
                # Normalize column names.
                cols = {c.lower().strip(): c for c in df.columns}
                tcol = next((cols[k] for k in ("datetime", "timestamp", "time",
                                               "date") if k in cols), None)
                if not tcol:
                    raise ValueError("No datetime/timestamp column found. "
                                     f"Columns: {list(df.columns)}")
                ocol = next((cols[k] for k in ("open", "o") if k in cols), None)
                hcol = next((cols[k] for k in ("high", "h") if k in cols), None)
                lcol = next((cols[k] for k in ("low", "l") if k in cols), None)
                ccol = next((cols[k] for k in ("close", "c") if k in cols), None)
                if not all([ocol, hcol, lcol, ccol]):
                    raise ValueError("Missing OHLC columns. "
                                     f"Columns: {list(df.columns)}")
                _job_update(jid, progress=30,
                            message=f"Parsing {len(df):,} rows...")
                # Parse timestamps -> UTC epoch seconds.
                from zoneinfo import ZoneInfo
                tzinfo = ZoneInfo(tz)
                ts = pd.to_datetime(df[tcol])
                if ts.dt.tz is None:
                    ts = ts.dt.tz_localize(tzinfo)
                ts_utc = ts.dt.tz_convert("UTC")
                out = pd.DataFrame({
                    "time": (ts_utc.astype("int64") // 10**9).astype("int64"),
                    "open": pd.to_numeric(df[ocol], errors="coerce"),
                    "high": pd.to_numeric(df[hcol], errors="coerce"),
                    "low": pd.to_numeric(df[lcol], errors="coerce"),
                    "close": pd.to_numeric(df[ccol], errors="coerce"),
                })
            out = out.dropna().drop_duplicates("time").sort_values(
                "time").reset_index(drop=True)
            imported = len(out)
            _job_update(jid, progress=60,
                        message=f"Parsed {imported:,} bars. Merging into "
                                f"{canon} database...")
            # Merge into main DB (file data wins for its timestamps).
            # All gold variants (XAUUSD/GOLD/GOLD#) share ONE database.
            safe = db_key_for(canon)
            # CSV-import merge target lives in the runtime dir
            # (DATA_DIR), never in the code tree.
            base = _rt_base() if _RUNTIME_DIR else _rt_cache_base()
            main_path = os.path.join(base, f"{safe}_1m.parquet")
            main_path = os.path.abspath(main_path)
            if os.path.exists(main_path):
                main_df = pd.read_parquet(main_path)
                combined = pd.concat([main_df, out]).drop_duplicates(
                    "time", keep="last").sort_values("time").reset_index(
                    drop=True)
            else:
                combined = out
            combined[["time", "open", "high", "low", "close"]].to_parquet(
                main_path, index=False)
            _job_update(jid, progress=100, status="done", done=True,
                        message=f"Import done: {imported:,} bars from file "
                                f"(as {canon}), {len(combined):,} total in database.")
        except Exception as e:
            _job_update(jid, status="error", done=True, error=str(e),
                        message=f"Import failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jid


# --------------------------------------------------------------------------
# Smart symbol recognition - XAUUSD/GOLD/GOLD# are the same gold.
# --------------------------------------------------------------------------

import re as _re

# Canonical -> list of broker variants (all map to the canonical).
_SYMBOL_MAP = {
    "XAUUSD": ["XAUUSD", "GOLD"],  # spot gold, any broker naming
}


def normalize_symbol(sym):
    """Map broker symbol variants to a canonical database key.

    XAUUSD, XAUUSD., XAUUSDm, GOLD, GOLD#, GOLDm -> XAUUSD.
    Unknown symbols: uppercased, suffix-stripped, used as-is.
    This is 'smart, not blind': all gold data lands in ONE database file
    regardless of what the broker calls it.
    """
    s = str(sym or "").upper().strip()
    # Strip common broker suffixes: . # _m _pro etc.
    s = _re.sub(r"[.#_]+(M|PRO|ECN|RAW|STD|CENT)?$", "", s)
    s = s.rstrip(".#_")
    # GOLD* is spot gold = XAUUSD.
    if s == "GOLD" or s.startswith("GOLD"):
        return "XAUUSD"
    if s == "XAUUSD" or s.startswith("XAUUSD"):
        return "XAUUSD"
    return s or "XAUUSD"


def symbol_from_filename(fname):
    """Extract symbol from a history filename (e.g. DAT_ASCII_XAUUSD_M1_2024)."""
    base = os.path.basename(fname).upper()
    # Look for known instrument patterns.
    m = _re.search(r"(XAUUSD|GOLD)[.#_MPROECN]*", base)
    if m:
        return normalize_symbol(m.group(1))
    m = _re.search(r"([A-Z]{6,7})", base)
    if m:
        return normalize_symbol(m.group(1))
    return "XAUUSD"


def db_key_for(symbol):
    """Filesystem-safe database key for a symbol (canonical form)."""
    canon = normalize_symbol(symbol)
    return "".join(c for c in canon if c.isalnum() or c in "_-")
