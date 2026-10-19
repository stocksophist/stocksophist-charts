#!/usr/bin/env python3
"""StockSophist History Database reader.

Reads the standalone XAUUSD history database from an EXTERNAL folder -
never bundled with the app code. The app only reads; the user manages the file.

Folder layout on the the host machine:
    C:/stocksophist/
    |-- app/ <- chart app code (updated via zips, never contains .db)
    |-- data/ <- DATABASE FOLDER (the user owns, app only reads)
        |-- stocksophist_xauusd_m1_*.db

Search order for the database file:
    1. STOCKSOPHIST_DATA_DIR environment variable
    2. C:/stocksophist/data (Windows default - the user's data folder)
    3. ../../data (sibling of the app root)
    4. ./data (inside the app tree - dev/legacy)
    5. ~/stocksophist_data

Priority: TICK data first, then M1. Both are merged into one seamless
candle series - tick-derived candles win wherever both exist.

Database schema (SQLite):
    m1 : time (sec epoch, UTC), open, high, low, close
    ticks : time (ms epoch, UTC), bid, ask
    meta : key/value (bars, ticks_count, range_start/end, ticks_range, ...)
"""
import os
import sqlite3
import glob
import threading

# --------------------------------------------------------------------------
# Database location
# --------------------------------------------------------------------------

def _candidate_dirs():
    """Yield directories to search for the history database, in order."""
    # 1. Explicit env var wins.
    env = os.environ.get("STOCKSOPHIST_DATA_DIR", "").strip()
    if env:
        yield os.path.expandvars(env)
    # 2. Default database folder on Windows: C:\stocksophist\data
    # (the data folder the user manages; app only reads from it).
    if os.name == "nt":
        yield r"C:\stocksophist\data"
    # 3. Sibling ../data of the APP ROOT.
    #    __file__ is <app_root>/app/history_db.py, so go up TWO levels.
    here = os.path.dirname(os.path.abspath(__file__))   # <app_root>/app
    app_root = os.path.dirname(here)                    # <app_root>
    yield os.path.join(os.path.dirname(app_root), "data")  # C:/stocksophist/data
    # 4. ./data inside the app tree (legacy / dev).
    yield os.path.join(here, "data")
    # 5. Home folder fallback.
    yield os.path.join(os.path.expanduser("~"), "stocksophist_data")


def find_database():
    """Return the path of the best history .db, or None.

    'Best' = the XAUUSD history file with the most M1 bars. Picks up any
    stocksophist_xauusd_*.db / *.db in the data folders.

    a manual override (set from the DATA panel, stored in
    settings.json as db_path_override) wins over auto-detection, and is
    checked first.
    """
    # 0. Manual override from the DATA panel (persisted in settings.json).
    try:
        import json as _json
        _sf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "settings.json")
        if os.path.exists(_sf):
            with open(_sf, "r") as _f:
                _s = _json.load(_f)
            _ov = (_s.get("db_path_override") or "").strip()
            if _ov and os.path.isfile(_ov):
                return _ov
    except Exception:
        pass
    best, best_bars = None, -1
    for d in _candidate_dirs():
        if not os.path.isdir(d):
            continue
        for pat in ("stocksophist_xauusd_*.db", "*.db"):
            for p in glob.glob(os.path.join(d, pat)):
                try:
                    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True,
                                          timeout=5)
                    row = con.execute(
                        "SELECT value FROM meta WHERE key='bars'").fetchone()
                    con.close()
                    bars = int(row[0]) if row else 0
                    if bars > best_bars:
                        best, best_bars = p, bars
                except Exception:
                    continue
    return best


def debug_search():
    """explain where the app looked and what it saw (for the
    DATA panel when the DB is not found)."""
    import json as _json
    out = {"override": None, "candidates": []}
    try:
        _sf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "settings.json")
        if os.path.exists(_sf):
            with open(_sf, "r") as _f:
                _s = _json.load(_f)
            _ov = (_s.get("db_path_override") or "").strip()
            out["override"] = {"path": _ov,
                               "exists": bool(_ov and os.path.isfile(_ov))}
    except Exception as e:
        out["override"] = {"error": str(e)}
    for d in _candidate_dirs():
        info = {"dir": d, "exists": os.path.isdir(d), "files": []}
        if info["exists"]:
            try:
                for pat in ("stocksophist_xauusd_*.db", "*.db"):
                    for p in glob.glob(os.path.join(d, pat)):
                        info["files"].append(
                            {"path": p,
                             "size_mb": round(os.path.getsize(p) / 1e6, 1)})
                    if info["files"]:
                        break
            except Exception as e:
                info["error"] = str(e)
        out["candidates"].append(info)
    return out


# --------------------------------------------------------------------------
# Timeframe helpers
# --------------------------------------------------------------------------

_TF_SEC = {"1s": 1, "15s": 15, "1m": 60, "3m": 180, "5m": 300, "15m": 900,
           "30m": 1800, "1H": 3600, "4H": 14400, "D": 86400,
           "W": 604800, "M": 2592000}


def _bucket_sec(ts_sec, tf):
    """Floor a seconds-epoch timestamp to its candle slot (UTC)."""
    if tf == "D":
        return (ts_sec // 86400) * 86400
    if tf == "W":
        # Monday 00:00 UTC anchor.
        return ((ts_sec - 345600) // 604800) * 604800 + 345600
    if tf == "M":
        import datetime
        dt = datetime.datetime.fromtimestamp(ts_sec,
                                             tz=datetime.timezone.utc)
        return int(datetime.datetime(dt.year, dt.month, 1,
                                     tzinfo=datetime.timezone.utc
                                     ).timestamp())
    sec = _TF_SEC.get(tf, 60)
    return (ts_sec // sec) * sec


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

class HistoryDB:
    """Thread-safe reader for the standalone history database."""

    def __init__(self, path=None):
        self.path = path or find_database()
        self._lock = threading.Lock()
        self._meta = {}
        self._m1_range = (None, None)
        self._tick_range = (None, None)
        if self.path:
            self._load_meta()

    # - introspection ----------------------------------------------------

    def available(self):
        return self.path is not None and os.path.exists(self.path)

    def _load_meta(self):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                                  timeout=10)
            for k, v in con.execute("SELECT key, value FROM meta"):
                self._meta[k] = v
            r = con.execute("SELECT MIN(time), MAX(time) FROM m1").fetchone()
            if r and r[0]:
                self._m1_range = (int(r[0]), int(r[1]))
            try:
                r = con.execute(
                    "SELECT MIN(time), MAX(time) FROM ticks").fetchone()
                if r and r[0]:
                    # ticks stored in ms -> convert to seconds
                    self._tick_range = (int(r[0]) // 1000, int(r[1]) // 1000)
            except sqlite3.OperationalError:
                pass  # no ticks table in older DBs
            con.close()
        except Exception:
            self.path = None

    def status(self):
        """JSON-friendly status for /api/history_status."""
        import datetime

        def fmt(ts):
            if not ts:
                return None
            return datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")

        size_mb = (os.path.getsize(self.path) / 1e6) if self.available() \
            else 0
        return {
            "available": self.available(),
            "path": self.path,
            "size_mb": round(size_mb, 1),
            "m1_bars": int(self._meta.get("bars", 0)),
            "m1_start": fmt(self._m1_range[0]),
            "m1_end": fmt(self._m1_range[1]),
            "tick_count": int(self._meta.get("ticks_count", 0)),
            "tick_start": fmt(self._tick_range[0]),
            "tick_end": fmt(self._tick_range[1]),
        }

    # - candle building ---------------------------------------------------

    def get_candles(self, tf, start_ts, end_ts, limit=25000):
        """Seamless candle series for [start_ts, end_ts] (seconds, UTC).

        Tick-derived candles take priority; M1 fills everything else.
        Returns oldest->newest dicts: time/open/high/low/close (+source).
        """
        if not self.available():
            return []
        if tf not in _TF_SEC:
            return []

        merged = {}  # time -> bar dict; ticks overwrite M1 on overlap

        # M1 first (fallback layer).
        m1_bars = self._m1_candles(tf, start_ts, end_ts)
        for b in m1_bars:
            b["source"] = "m1"
            merged[b["time"]] = b

        # Ticks on top (priority layer).
        tick_bars = self._tick_candles(tf, start_ts, end_ts)
        for b in tick_bars:
            b["source"] = "tick"
            merged[b["time"]] = b

        bars = [merged[k] for k in sorted(merged)]
        if limit and len(bars) > limit:
            bars = bars[-limit:]
        return bars

    def newest_time(self):
        """Newest timestamp (seconds) covered by either table."""
        cands = [t for t in (self._m1_range[1], self._tick_range[1]) if t]
        return max(cands) if cands else None

    # - internals ----------------------------------------------------------

    def _query(self, sql, params):
        with self._lock:
            con = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                                  timeout=30)
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()
        return rows

    def _m1_candles(self, tf, start_ts, end_ts):
        ms, me = self._m1_range
        if not ms:
            return []
        s = max(start_ts, ms)
        e = min(end_ts, me)
        if s > e:
            return []
        rows = self._query(
            "SELECT time, open, high, low, close FROM m1 "
            "WHERE time BETWEEN ? AND ? ORDER BY time", (s, e))
        if not rows:
            return []
        buckets = {}
        for t, o, h, l, c in rows:
            b = _bucket_sec(int(t), tf)
            if b not in buckets:
                buckets[b] = [float(o), float(h), float(l), float(c)]
            else:
                prev = buckets[b]
                prev[1] = max(prev[1], float(h))
                prev[2] = min(prev[2], float(l))
                prev[3] = float(c)
        # the m1 table stores no volume -> 0 (unknown).
        return [{"time": k, "open": v[0], "high": v[1], "low": v[2],
                 "close": v[3], "volume": 0}
                for k, v in sorted(buckets.items())]

    def _tick_candles(self, tf, start_ts, end_ts):
        ts, te = self._tick_range
        if not ts:
            return []
        s = max(start_ts, ts)
        e = min(end_ts, te)
        if s > e:
            return []
        # ticks.time is ms epoch
        rows = self._query(
            "SELECT time, bid, ask FROM ticks "
            "WHERE time BETWEEN ? AND ? ORDER BY time", (s * 1000, e * 1000))
        if not rows:
            return []
        buckets = {}
        for tms, bid, ask in rows:
            t = int(tms) // 1000
            b = _bucket_sec(t, tf)
            bid, ask = float(bid), float(ask)
            if b not in buckets:
                buckets[b] = [bid, max(bid, ask), min(bid, ask), bid, 1]
            else:
                prev = buckets[b]
                prev[1] = max(prev[1], bid, ask)
                prev[2] = min(prev[2], bid, ask)
                prev[3] = bid
                prev[4] += 1
        # tick count per bucket is the tick volume MT5 would report.
        return [{"time": k, "open": v[0], "high": v[1], "low": v[2],
                 "close": v[3], "volume": v[4]}
                for k, v in sorted(buckets.items())]


# Module-level singleton, lazily created.
_DB = None
_DB_LOCK = threading.Lock()


def get_db():
    global _DB
    with _DB_LOCK:
        if _DB is None:
            _DB = HistoryDB()
        return _DB


def refresh():
    """Forget the cached DB handle (call after the user drops in a new file)."""
    global _DB
    with _DB_LOCK:
        _DB = None
