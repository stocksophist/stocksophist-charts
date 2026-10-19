"""StockSophist Charts - local tick vault.

Every MT5 quote the server sees is persisted here as 1-second OHLC bars in
SQLite (app/data/ticks.db). Why: MT5's *rate history* can freeze or skip
chunks while its *tick stream* keeps flowing (the frozen-candle reports).
The vault is our own recording of the same ticks the live price line is drawn
from, so when MT5's history has a hole the chart heals the gap from the vault
instead of leaving blank space.

- Granularity: 1 second (exactly what was asked: "whatever less is possible,
  else 1 sec").
- Retention: 14 days of seconds per symbol (tune TICK_RETENTION_DAYS below).
  Older seconds are pruned automatically.
- Thread-safe; never raises out of record() - a vault failure must never
  break the quote path.
- Nothing is ever fabricated: slots with no recorded seconds stay missing.
"""

import atexit
import os
import sqlite3
import threading
import time

# Days of 1-second bars kept per symbol. Older data is pruned on startup and
# once a day. ~86k rows/day/symbol; 14 days is plenty for gap healing (gaps
# are normally minutes/hours, not weeks).
TICK_RETENTION_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS s1(
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
  n INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(symbol, ts));
"""

_FLUSH_BATCH = 300       # flush to disk after this many completed seconds ...
_FLUSH_IDLE_S = 30       # .. or after this many seconds since the last flush
_OPEN_MAX_AGE_S = 5      # an "open" second older than this is closed+flushed


class TickVault:
    def __init__(self, data_dir, retention_days=TICK_RETENTION_DAYS):
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._open = {}      # symbol -> [sec, o, h, l, c, n]
        self._pending = []   # completed seconds awaiting INSERT
        self._last_flush = time.time()
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "ticks.db")
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._db.executescript(_SCHEMA)
        self._migrate_time_basis()
        atexit.register(self.close)

    def _migrate_time_basis(self):
        """tick timestamps are now true UTC (the feed converts MT5's
        server-basis times at the boundary). A vault written by an older
        build holds server-basis seconds - up to 14h in the future - which
        would corrupt every bar built from it. Detect (newest row far ahead
        of the UTC wall clock) and archive the old file aside instead of
        mixing the two bases; a timestamped archive keeps the raw data."""
        try:
            row = self._db.execute("SELECT MAX(ts) FROM s1").fetchone()
            newest = row[0] if row else None
            if newest and int(newest) > int(time.time()) + 3600:
                try:
                    self._db.close()
                except Exception:
                    pass
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                arc = self.path + ".serverbasis-" + stamp + ".bak"
                try:
                    os.replace(self.path, arc)
                except Exception:
                    pass
                self._db = sqlite3.connect(self.path, check_same_thread=False)
                try:
                    self._db.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
                self._db.executescript(_SCHEMA)
        except Exception:
            pass  # never break startup over the vault

    # ------------------------------------------------------------ recording
    def record(self, symbol, bid, ts=None):
        """Record one tick (bid price). Buckets into 1-second OHLC."""
        try:
            px = float(bid)
            sec = int(ts) if ts else int(time.time())
        except (TypeError, ValueError):
            return
        if not symbol or px <= 0 or sec <= 0:
            return
        try:
            with self._lock:
                cur = self._open.get(symbol)
                if cur is None or cur[0] != sec:
                    if cur is not None:
                        self._pending.append(
                            (symbol, cur[0], cur[1], cur[2], cur[3], cur[4], cur[5]))
                    self._open[symbol] = [sec, px, px, px, px, 1]
                else:
                    if px > cur[2]:
                        cur[2] = px
                    if px < cur[3]:
                        cur[3] = px
                    cur[4] = px
                    cur[5] += 1
                if (len(self._pending) >= _FLUSH_BATCH
                        or time.time() - self._last_flush > _FLUSH_IDLE_S):
                    self._flush_locked()
        except Exception:
            pass  # the vault must never break the quote path

    def idle_flush(self):
        """Close+flush any open second that has been idle (call ~1/sec from
        the recorder thread so the last second of a quiet market is persisted
        instead of sitting in memory)."""
        try:
            with self._lock:
                now = int(time.time())
                for sym in list(self._open):
                    cur = self._open[sym]
                    if now - cur[0] >= _OPEN_MAX_AGE_S:
                        self._pending.append(
                            (sym, cur[0], cur[1], cur[2], cur[3], cur[4], cur[5]))
                        del self._open[sym]
                self._flush_locked()
        except Exception:
            pass

    def _flush_locked(self):
        if not self._pending:
            self._last_flush = time.time()
            return
        try:
            self._db.executemany(
                "INSERT OR REPLACE INTO s1(symbol,ts,o,h,l,c,n) VALUES(?,?,?,?,?,?,?)",
                self._pending)
            self._db.commit()
        except Exception:
            try:
                self._db.rollback()
            except Exception:
                pass
        self._pending = []
        self._last_flush = time.time()

    def close(self):
        """Flush everything (including open seconds) and close the DB."""
        try:
            with self._lock:
                for sym, cur in self._open.items():
                    self._pending.append(
                        (sym, cur[0], cur[1], cur[2], cur[3], cur[4], cur[5]))
                self._open = {}
                self._flush_locked()
                self._db.close()
        except Exception:
            pass

    # -------------------------------------------------------------- reading
    def fetch(self, symbol, start_ts, end_ts):
        """1-second bars for symbol in [start_ts, end_ts], oldest -> newest.
        Merges disk rows, completed-but-unflushed seconds, and the still-open
        in-memory second, so the freshest ticks are always visible."""
        rows = []
        try:
            with self._lock:
                q = self._db.execute(
                    "SELECT ts,o,h,l,c,n FROM s1 WHERE symbol=? AND ts>=? AND ts<=?"
                    " ORDER BY ts", (symbol, int(start_ts), int(end_ts)))
                rows = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                         "close": r[4], "ticks": r[5]} for r in q.fetchall()]
                for p in self._pending:
                    if p[0] == symbol and start_ts <= p[1] <= end_ts:
                        rows.append({"time": p[1], "open": p[2], "high": p[3],
                                     "low": p[4], "close": p[5], "ticks": p[6]})
                cur = self._draw_open(symbol, start_ts, end_ts)
                if cur:
                    rows.append(cur)
                # Dedupe by second: disk < pending < open (newest view wins;
                # values are identical anyway - INSERT OR REPLACE semantics).
                ded = {}
                for r in rows:
                    ded[r["time"]] = r
                rows = [ded[k] for k in sorted(ded)]
        except Exception:
            pass
        return rows

    def _draw_open(self, symbol, start_ts, end_ts):
        cur = self._open.get(symbol)
        if cur and start_ts <= cur[0] <= end_ts:
            return {"time": cur[0], "open": cur[1], "high": cur[2],
                    "low": cur[3], "close": cur[4], "ticks": cur[5]}
        return None

    def build_bars(self, symbol, start_ts, end_ts, tf_sec):
        """Aggregate 1-second bars into timeframe bars over [start_ts, end_ts].

        Returns [{time, open, high, low, close, local: True}] - one bar per
        tf slot that has at least one recorded second. Slots with no seconds
        are omitted (never fabricated)."""
        secs = self.fetch(symbol, start_ts, end_ts)
        groups = {}
        for s in secs:
            slot = (s["time"] // tf_sec) * tf_sec
            g = groups.get(slot)
            if g is None:
                groups[slot] = [s["open"], s["high"], s["low"], s["close"]]
            else:
                if s["high"] > g[1]:
                    g[1] = s["high"]
                if s["low"] < g[2]:
                    g[2] = s["low"]
                g[3] = s["close"]
        bars = [{"time": slot, "open": round(g[0], 3), "high": round(g[1], 3),
                 "low": round(g[2], 3), "close": round(g[3], 3),
                 "volume": 0, "local": True}
                for slot, g in groups.items()]
        bars.sort(key=lambda b: b["time"])
        return bars

    def newest(self, symbol):
        """Newest recorded second for symbol (memory first, then disk)."""
        try:
            with self._lock:
                cur = self._open.get(symbol)
                mem = cur[0] if cur else 0
                q = self._db.execute(
                    "SELECT MAX(ts) FROM s1 WHERE symbol=?", (symbol,))
                row = q.fetchone()
                disk = row[0] if row and row[0] else 0
                return max(mem, disk)
        except Exception:
            return 0

    def status(self, symbol=None):
        """Small health snapshot for /api/status."""
        out = {"db_mb": 0.0, "retention_days": self.retention_days}
        try:
            out["db_mb"] = round(os.path.getsize(self.path) / 1048576, 2)
        except Exception:
            pass
        if symbol:
            try:
                now = int(time.time())
                with self._lock:
                    q = self._db.execute(
                        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM s1"
                        " WHERE symbol=? AND ts>=?", (symbol, now - 86400))
                    n, mn, mx = q.fetchone()
                    n = int(n or 0)
                    # plus completed-but-unflushed and open in-memory seconds
                    for p in self._pending:
                        if p[0] == symbol and p[1] >= now - 86400:
                            n += 1
                    cur = self._open.get(symbol)
                    if cur and cur[0] >= now - 86400:
                        n += 1
                    mx = max(mx or 0, cur[0] if cur else 0)
                out.update({"symbol": symbol, "seconds_24h": n,
                            "oldest": mn, "newest": mx})
            except Exception:
                pass
        return out

    # ---------------------------------------------------------- maintenance
    def prune(self):
        """Delete seconds older than the retention window."""
        try:
            cutoff = int(time.time()) - int(self.retention_days * 86400)
            with self._lock:
                self._db.execute("DELETE FROM s1 WHERE ts < ?", (cutoff,))
                self._db.commit()
        except Exception:
            pass
