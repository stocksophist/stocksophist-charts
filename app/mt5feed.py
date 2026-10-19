"""
MT5 data feed with graceful DEMO fallback.

On the the host machine (Windows + MT5 terminal installed and logged in) this talks to
MetaTrader5 directly:
  - M1 history via copy_rates_from_pos (fast, deep history)
  - tick-by-tick via copy_ticks_from (COPY_TICKS_ALL = every price change)
  - live quote via symbol_info_tick

On any machine WITHOUT MetaTrader5 (dev/testing here), it falls back to DEMO
mode and serves the Backtest Lab's cleaned XAUUSD M1 parquet so the whole
chart pipeline can be tested end-to-end without a broker connection.
"""

import os
import time
import threading
from datetime import datetime, timedelta, timezone

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False

import pandas as pd

DEMO_PARQUET_DIR = None  # set by server.py when running in demo mode

# Broker gold symbols to try, in order. The engine already pins GOLD# for live
# trading; the chart lets the user type any symbol, this is just auto-detect.
_SYMBOL_CANDIDATES = ["XAUUSD", "GOLD#", "GOLD", "XAUUSDm", "XAUUSD.", "XAUUSD#"]


# max bars pulled per MT5 call when backfilling deep history
_DEEP_CHUNK = 50000
# max ticks processed in one sub-minute backfill (bounds CPU/RAM)
_MAX_TICKS = 1000000

# the mt5 package's IPC is not documented thread-safe, and
# Flask serves threaded=True. Concurrent copy_ticks_from bursts (chart
# tickbars + N indicator pulls + Renko chain builds at a mode switch)
# returned None / raised under load. One lock serializes the tick pulls;
# the cached base series means the deep pull now happens rarely.
_TICK_PULL_LOCK = threading.Lock()
# never wait forever for the tick-pull lock - a previous pull stuck
# on a dead terminal must not wedge every later tick request.
_TICK_PULL_LOCK_TIMEOUT = 60


def _bounded_call(fn, timeout_s, what):
    """Run an MT5 call in a daemon worker; give up after timeout_s.

    sync_watchlist's symbols_get / symbol_select must degrade to
    an honest timeout error, never wedge the sync op. A worker wedged in a
    C-level MT5 call cannot be killed - it lingers as a daemon while the
    caller moves on with a real error."""
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
        raise RuntimeError(
            "%s timed out after %ss - MT5 is not responding "
            "(terminal may still be syncing)" % (what, timeout_s))
    if "err" in out:
        raise out["err"]
    return out["res"]


class Feed:
    def __init__(self, parquet_dir=None, conn=None):
        global DEMO_PARQUET_DIR
        self.conn = conn  # MT5Connection (mt5conn.py) or None
        # cache fields FIRST - self.live (a property) reads them,
        # and __init__ touches self.live two lines below.
        self._live_cache = False
        self._live_at = 0.0
        self._live_key = None
        self._sym_cache = None
        self._sym_at = 0.0
        self.tz_label = ""
        if self.live:
            self.tz_label = "MT5 live (UTC)"
        else:
            DEMO_PARQUET_DIR = parquet_dir
            self.tz_label = "demo data (IST)"
        self._demo_cache = None
        self._demo_mtime = 0
        self._resolve_cache = {}  # requested symbol -> (resolved, note, ts)
        self._prev_close_cache = {}  # symbol -> (utc-date, prev daily close)
        self._qseq = {}          # symbol -> monotonic quote sequence (ordering guard)
        self._off = 0            # broker server-time minus UTC, seconds
        self._off_at = 0.0       # wall-clock when the offset was last derived
        self._hist_fail = 0      # consecutive MT5 history (copy_rates) failures
        self._cb_state = "closed"  # circuit breaker: closed|open|half-open
        self._cb_opened_at = 0.0
        self._cb_probe_at = 0.0
        # liveness + symbol detection are MT5 IPC - on a sick
        # terminal each call can hang for seconds, and status endpoints
        # touch them on every poll. Cache briefly so a browser refresh
        # (or the 15s badge poll) never queues behind a dead terminal.
        # NOTE: the fields themselves are initialized at the TOP of
        # __init__ (before the first self.live access); see above.

    # cached liveness. 10s TTL - fresh enough for badges and
    # the startup gate, cheap enough that no status poll ever blocks on
    # a hung terminal_info()/account_info() IPC round-trip.
    _LIVE_TTL = 10
    _SYM_TTL = 300

    def _probe_live(self):
        try:
            return bool(self.conn is not None and self.conn.is_live())
        except Exception:
            return False

    @property
    def live(self):
        """Live = connection manager says the terminal + broker session is up.

        cached 10s - is_live() is MT5 IPC and a sick terminal
        makes every call hang; uncached, every /api/status poll and every
        browser refresh queued behind it. The cache key includes the conn
        identity and its _live flag, so a connect/disconnect (or a conn
        swap) re-probes immediately instead of serving stale state."""
        conn = self.conn
        key = (id(conn), getattr(conn, "_live", None) if conn else None)
        now = time.monotonic()
        if key == self._live_key and now - self._live_at < self._LIVE_TTL:
            return self._live_cache
        v = self._probe_live()
        self._live_cache = v
        self._live_at = now
        self._live_key = key
        return v

    def live_fresh(self):
        """Bypass the cache - for the few places that need the truth now
        (startup gate re-checks, manual reconnect)."""
        v = self._probe_live()
        self._live_cache = v
        self._live_at = time.monotonic()
        self._live_key = (id(self.conn),
                          getattr(self.conn, "_live", None)
                          if self.conn else None)
        return v

    # -------------------------------- MT5 history health
    # The frozen-candle failure mode is a half-dead terminal: quotes keep
    # flowing while copy_rates returns frozen/empty bars. Count consecutive
    # history failures so the UI can say "MT5 history degraded" instead of
    # silently serving old bars forever.
    def note_hist_ok(self):
        self._hist_fail = 0
        self._cb_state = "closed"

    def note_hist_fail(self):
        self._hist_fail = min(self._hist_fail + 1, 999)
        if self._cb_state == "half-open" or self._hist_fail >= 5:
            self._cb_state = "open"
            self._cb_opened_at = time.monotonic()

    @property
    def degraded(self):
        return self._hist_fail >= 5

    # -------------------------------- MT5 history circuit breaker
    # A sick terminal (post sleep/wake, still syncing) makes every history
    # call burn its full timeout (30s tail + 30s gapfill per /api/candles),
    # so a browser refresh "stops working" for a minute+. The breaker opens
    # after 5 consecutive history failures: history calls are then skipped
    # and the disk/memory cache is served instantly. Every 5 minutes a
    # single probe is let through; success closes the breaker, failure
    # re-opens it. Quotes are unaffected (separate path).
    _CB_OPEN_SECS = 300
    _CB_PROBE_STALE_SECS = 180

    def hist_mt5_allowed(self):
        """True if the caller may attempt an MT5 history fetch now.

        closed -> True. open -> False until 5 min pass, then one caller
        becomes the half-open probe (True). half-open -> False for everyone
        else (a stale probe slot is reclaimable after 3 min).
        """
        now = time.monotonic()
        if self._cb_state == "closed":
            return True
        if self._cb_state == "open":
            if now - self._cb_opened_at >= self._CB_OPEN_SECS:
                self._cb_state = "half-open"
                self._cb_probe_at = now
                return True
            return False
        if now - self._cb_probe_at >= self._CB_PROBE_STALE_SECS:
            self._cb_probe_at = now
            return True
        return False

    @property
    def hist_circuit(self):
        """closed | open | half-open - for the status UI."""
        return self._cb_state

    # ------------------------------------------ MT5 server time -> UTC
    # MT5 bar/tick `time` fields are seconds-since-epoch in the TRADE
    # SERVER's zone (e.g. the broker = UTC+3 in September), NOT UTC. The frontend,
    # tick vault, Renko chain and alert engine all assume true UTC unix time,
    # so the raw values shifted every displayed time by the broker offset and
    # silently disabled the STALE badge (q.time sat hours in the future, so
    # serverNow - q.time was always negative). The offset is DERIVED
    # empirically, not assumed: (server clock - UTC wall clock) rounded to a
    # 15-minute grid. If a broker ever returns true UTC epochs, the measured
    # delta rounds to 0 and to_utc() becomes a harmless no-op - safe either
    # way. Re-derived hourly so broker DST switches are picked up.
    def _measure_offset(self):
        """Measure MT5-clock-minus-UTC (seconds, 15-min grid), or None.

        Cross-validates TWO independent MT5-clock readings - the latest tick
        time and the newest M1 bar time. Both live in MT5's clock domain, so
        they must imply the same offset vs the wall clock. If they disagree
        by more than one grid step the terminal is sick (stale tick, frozen
        history) and the measurement is rejected. Uses raw MT5 calls (no
        _df_to_utc) to avoid recursing into utc_offset().
        """
        if not self.live or mt5 is None:
            return 0
        try:
            sym = self.detect_symbol()
            if not sym:
                return None
            now = time.time()
            offs = []
            try:
                t = mt5.symbol_info_tick(sym)
                if t is not None and getattr(t, "time", 0):
                    offs.append(int(round((int(t.time) - now) / 900.0)) * 900)
            except Exception:
                pass
            try:
                rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 1)
                if rates is not None and len(rates):
                    # Newest slot midpoint (forming bar) ~= server "now".
                    offs.append(int(round((int(rates[0]["time"]) + 30 - now) / 900.0)) * 900)
            except Exception:
                pass
            if not offs:
                return None
            if len(offs) == 2 and abs(offs[0] - offs[1]) > 900:
                return None  # MT5's own clocks disagree - don't trust either
            off = offs[0]
            if abs(off) > 14 * 3600:
                return None
            return off
        except Exception:
            return None

    def utc_offset(self):
        """Broker server-time minus UTC, in seconds. 0 when unknown/demo.

        the measurement is now cross-validated (tick clock vs
        newest-bar clock) and a rejected measurement is NEVER cached - the
        old code could lock in a wrong offset for a full hour when MT5
        handed back a stale tick on the first read, shifting every bar
        (wrong axis times, broken day-boundary indicators).
        """
        if not self.live or mt5 is None:
            return 0
        now = time.time()
        if now - self._off_at < 3600:
            return self._off
        off = self._measure_offset()
        if off is not None:
            self._off, self._off_at = off, now
        # else: keep the last good value (or 0) and retry on the next call -
        # never cache a failure.
        return self._off

    def resync_time(self):
        """Force a fresh offset measurement on the next utc_offset() call."""
        self._off_at = 0
        return self.utc_offset()

    def to_utc(self, ts):
        """Convert one MT5 server-basis timestamp to true UTC unix seconds."""
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return ts
        off = self.utc_offset()
        return ts - off if off else ts

    def _df_to_utc(self, df):
        """Shift a live DataFrame's `time` column from server basis to UTC."""
        off = self.utc_offset()
        if off and "time" in df.columns:
            df = df.copy()
            df["time"] = (df["time"].astype("int64") - off).astype("int64")
        return df

    def _need_demo(self):
        import glob as _glob
        if not DEMO_PARQUET_DIR or not _glob.glob(
                os.path.join(DEMO_PARQUET_DIR, "XAUUSD_1m_*.parquet")):
            raise RuntimeError(
                "MT5 is not connected and no demo data is available. "
                "Open the MT5 button (top bar) and connect your terminal first.")

    # ----------------------------------------------------------------- symbols
    def detect_symbol(self):
        """Return the broker's gold symbol, or XAUUSD in demo mode.

        cached 5 min - every call walks mt5.symbol_info() IPC,
        and on a sick terminal that hangs. The broker's gold symbol does
        not change intraday; status endpoints call this on every poll."""
        now = time.monotonic()
        if self._sym_cache and now - self._sym_at < self._SYM_TTL:
            return self._sym_cache
        sym = self._detect_symbol_uncached()
        if sym:
            self._sym_cache = sym
            self._sym_at = now
        return sym or "XAUUSD"

    def _detect_symbol_uncached(self):
        if self.live:
            for name in _SYMBOL_CANDIDATES:
                info = mt5.symbol_info(name)
                if info is not None:
                    return name
            # last resort: first visible symbol containing XAU or GOLD
            for s in mt5.symbols_get("*XAU*") or []:
                return s.name
            for s in mt5.symbols_get("*GOLD*") or []:
                return s.name
            return "XAUUSD"
        return "XAUUSD"

    def resolve_symbol(self, requested):
        """Broker-aware symbol resolution.

        When MT5 is live and the requested symbol does not exist on the
        broker at all (e.g. pane asks for GOLD but the broker lists GOLD#),
        fall back to the detected broker gold symbol instead of failing and
        silently serving stale demo data. Returns (symbol, note); note is
        None when no substitution happened. Resolution is cached briefly
        because /api/quote calls this every second per pane.
        """
        s = (requested or "").strip() or self.detect_symbol()
        if not self.live or mt5 is None or not s:
            return s, None
        now = time.time()
        hit = self._resolve_cache.get(s)
        if hit and now - hit[2] < 60:
            return hit[0], hit[1]
        note = None
        try:
            info = mt5.symbol_info(s)
            if info is None:
                try:
                    mt5.symbol_select(s, True)
                    info = mt5.symbol_info(s)
                except Exception:
                    info = None
            if info is None:
                det = self.detect_symbol()
                if det and det != s:
                    note = "no %s on broker; using %s" % (s, det)
                    s = det
        except Exception:
            pass
        self._resolve_cache[requested or ""] = (s, note, now)
        return s, note

    # ----------------------------------------------------------------- candles
    def get_m1(self, symbol, count=3000):
        """Newest-first-safe DataFrame: time, open, high, low, close (unix secs)."""
        if self.live:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, count)
            if rates is None or len(rates) == 0:
                raise RuntimeError(f"MT5 returned no M1 data for {symbol}")
            df = pd.DataFrame(rates)
            df = df[["time", "open", "high", "low", "close",
                      "tick_volume"]].copy()
            df = df.rename(columns={"tick_volume": "volume"})
            df = df.sort_values("time").reset_index(drop=True)
            return self._df_to_utc(df)
        return self._demo_m1(count)

    def get_m1_deep(self, symbol, target=20000):
        """Deep M1 history (premium style): newest `target` bars.

        Live: walks back through the terminal in chunks until `target` is
        reached or the broker's history runs out. How far back you get is
        decided by the broker's server depth and the terminal's
        Tools -> Options -> Charts -> "Max bars in chart" setting - raise it
        (or set "unlimited") for deeper history.
        Demo: tail of the bundled 4-year M1 parquet."""
        if self.live:
            return self._walk_back(symbol, mt5.TIMEFRAME_M1, target)
        return self._demo_m1(target)

    def _walk_back(self, symbol, tf_const, target):
        """Pull up to `target` newest bars via chunked copy_rates_from_pos."""
        frames = []
        got = 0
        pos = 0
        while got < target:
            n = min(_DEEP_CHUNK, target - got)
            rates = mt5.copy_rates_from_pos(symbol, tf_const, pos, n)
            if rates is None or len(rates) == 0:
                break
            frames.append(pd.DataFrame(rates)[
                ["time", "open", "high", "low", "close", "tick_volume"]]
                .rename(columns={"tick_volume": "volume"}))
            got += len(rates)
            if len(rates) < n:
                break  # broker history exhausted
            pos += n
        if not frames:
            raise RuntimeError(f"MT5 returned no data for {symbol}")
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates("time").sort_values("time").reset_index(drop=True)
        return self._df_to_utc(out.tail(target).reset_index(drop=True))

    def _demo_m1(self, count):
        import os
        import glob
        self._need_demo()
        if self._demo_cache is None:
            # load the full history once (~1.4M M1 rows); tail() slices per call
            files = sorted(glob.glob(os.path.join(DEMO_PARQUET_DIR, "XAUUSD_1m_*.parquet")),
                           reverse=True)
            if not files:
                raise RuntimeError("no demo parquet found")
            frames = [pd.read_parquet(fp) for fp in files]
            df = pd.concat(frames, ignore_index=True)
            # normalise column names: time,o,h,l,c -> time,open,high,low,close
            rename = {"o": "open", "h": "high", "l": "low", "c": "close",
                      "O": "open", "H": "high", "L": "low", "C": "close"}
            df = df.rename(columns=rename)
            if "time" not in df.columns:
                # first column is the timestamp
                df = df.rename(columns={df.columns[0]: "time"})
            ts = pd.to_datetime(df["time"])
            if ts.dt.tz is None:
                ts = ts.dt.tz_localize("Asia/Kolkata")
            ts = ts.dt.tz_convert("UTC")
            # datetime64 unit may be s/ms/us/ns depending on file -> normalise
            try:
                df["time"] = ts.dt.as_unit("s").astype("int64")
            except Exception:
                df["time"] = (ts.astype("int64") // 10 ** 9).astype(int)
            df["volume"] = 0  # demo bars carry no volume
            df = df[["time", "open", "high", "low", "close",
                     "volume"]].sort_values("time")
            df = df.drop_duplicates("time").reset_index(drop=True)
            self._demo_cache = df
        return self._demo_cache.tail(count).reset_index(drop=True)

    @staticmethod
    def resample(df, tf_min):
        """1m -> N-minute candles."""
        if tf_min <= 1:
            return df
        d = df.copy()
        d["grp"] = (d["time"] // (tf_min * 60)) * (tf_min * 60)
        g = d.groupby("grp")
        out = pd.DataFrame({
            "time": g["grp"].first(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
        }).reset_index(drop=True)
        # tick volume sums across the resampled slot (activity proxy).
        if "volume" in d.columns:
            out["volume"] = g["volume"].sum().values
        else:
            out["volume"] = 0
        return out[["time", "open", "high", "low", "close", "volume"]]

    # -------------------------------------------- higher TFs: 15m .. Month
    _NATIVE_TF = {"15m": "M15", "30m": "M30", "1H": "H1", "4H": "H4",
                  "D": "D1", "W": "W1", "M": "MN1"}

    def get_rates_range(self, symbol, tf_name, start_ts, end_ts):
        """bars for an exact [start_ts, end_ts] unix-seconds window.

        Surgically fills interior gaps baked into the disk
        cache (e.g. a chunked deep pull ran while the terminal was still
        syncing history). Live only; returns [] when MT5 has nothing.
        """
        if not self.live:
            return []
        import datetime as _dt
        if tf_name in self._NATIVE_TF:
            tf_const = getattr(mt5, "TIMEFRAME_" + self._NATIVE_TF[tf_name])
        else:
            tf_const = mt5.TIMEFRAME_M1
        t0 = _dt.datetime.fromtimestamp(int(start_ts), tz=_dt.timezone.utc)
        t1 = _dt.datetime.fromtimestamp(int(end_ts), tz=_dt.timezone.utc)
        try:
            rates = mt5.copy_rates_range(symbol, tf_const, t0, t1)
        except Exception:
            return []
        if rates is None or len(rates) == 0:
            return []
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close",
                                   "tick_volume"]].copy()
        df = df.rename(columns={"tick_volume": "volume"})
        df = self._df_to_utc(df.sort_values("time").reset_index(drop=True))
        if tf_name not in self._NATIVE_TF and tf_name != "1m":
            mult = {"3m": 3, "5m": 5}.get(tf_name)
            if mult:
                df = Feed.resample(df, mult)
        return df

    def get_rates(self, symbol, tf_name, count=600):
        """15m/30m/1H/4H/D/W/M candles.
        Live: native MT5 timeframe (fast, broker-aligned). Demo: resampled M1."""
        if self.live:
            tf = getattr(mt5, "TIMEFRAME_" + self._NATIVE_TF[tf_name])
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None or len(rates) == 0:
                raise RuntimeError(f"MT5 returned no {tf_name} data for {symbol}")
            df = pd.DataFrame(rates)[["time", "open", "high", "low", "close",
                                   "tick_volume"]].copy()
            df = df.rename(columns={"tick_volume": "volume"})
            return self._df_to_utc(df.sort_values("time").reset_index(drop=True))
        return self._demo_resample(tf_name, count)

    def get_rates_deep(self, symbol, tf_name, target=20000):
        """Deep history for 15m/30m/1H/4H/D/W/M (newest `target` bars).
        Live: chunked walk-back on the native MT5 timeframe (broker-aligned).
        Demo: resampled from the full M1 parquet."""
        if self.live:
            tf = getattr(mt5, "TIMEFRAME_" + self._NATIVE_TF[tf_name])
            return self._walk_back(symbol, tf, target)
        return self._demo_resample(tf_name, target)

    def _demo_resample(self, tf_name, count):
        df = self._demo_m1(10 ** 9)  # full history; tail() below caps it
        d = df.copy()
        if tf_name == "M":
            d["dt"] = pd.to_datetime(d["time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            d["grp"] = d["dt"].dt.to_period("M").astype(str)
        else:
            minutes = {"15m": 15, "30m": 30, "1H": 60, "4H": 240,
                       "D": 1440, "W": 10080}[tf_name]
            d["grp"] = (d["time"] // (minutes * 60)) * (minutes * 60)
        g = d.groupby("grp", sort=True)
        out = pd.DataFrame({
            "time": g["time"].first(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
        }).reset_index(drop=True)
        out["volume"] = 0  # demo bars carry no volume
        return out.tail(count).reset_index(drop=True)

    # ------------------------------------------------ sub-minute from ticks
    def get_tickbars(self, symbol, sec, n_bars=400):
        """1s/15s bars aggregated from real MT5 tick history (bid).
        MT5 has no sub-minute rate feed, so ticks are bucketed directly.
        Tick depth is broker-limited (usually days-weeks): we request a window
        sized to the bar count and return however many bars the broker has.
        Demo M1 cannot fabricate seconds -> honest error."""
        if not self.live:
            raise RuntimeError("1s/15s needs live MT5 tick data (DEMO has M1 only)")
        if sec not in (1, 15):
            raise ValueError("sec must be 1 or 15")
        # window sized to the request: ~20 ticks per expected bar, capped.
        # (copy_ticks_from takes date_from + count; starting AT now can return
        # nothing, so we start in the past and let MT5 walk forward.)
        span_s = n_bars * sec
        from_dt = datetime.now(timezone.utc) - timedelta(seconds=span_s * 4 + 3600)
        count = min(_MAX_TICKS, max(50000, int(span_s * 20)))
        # the lock must not be waited on forever - a previous pull
        # stuck on a dead terminal would wedge every later tick request.
        if not _TICK_PULL_LOCK.acquire(timeout=_TICK_PULL_LOCK_TIMEOUT):
            raise RuntimeError(
                "MT5 tick history is not responding - terminal may still be syncing")
        try:
            ticks = mt5.copy_ticks_from(symbol, from_dt, count, mt5.COPY_TICKS_ALL)
        finally:
            _TICK_PULL_LOCK.release()
        if ticks is None or len(ticks) == 0:
            raise RuntimeError(f"no tick history for {symbol}")
        bars = {}
        for t in ticks:
            bt = (self.to_utc(int(t["time"])) // sec) * sec
            px = float(t["bid"])
            b = bars.get(bt)
            if b is None:
                bars[bt] = {"time": bt, "open": px, "high": px, "low": px,
                            "close": px, "volume": 1}
            else:
                if px > b["high"]:
                    b["high"] = px
                if px < b["low"]:
                    b["low"] = px
                b["close"] = px
                b["volume"] += 1
        out = [bars[k] for k in sorted(bars)][-n_bars:]
        if not out:
            raise RuntimeError(f"no tick history for {symbol}")
        return out, self.get_quote(symbol)

    # ----------------------------------------------------------------- ticks
    def get_ticks(self, symbol, count=2000, lookback_hours=6):
        """
        Tick-by-tick: list of {time, bid, ask}. MT5 COPY_TICKS_ALL = every price
        change (not just trades). Depth is broker-limited; recent hours are fine.
        Demo mode synthesises ticks from M1 closes (marked synthetic).
        """
        if self.live:
            to_dt = datetime.now(timezone.utc)
            from_dt = to_dt - timedelta(hours=lookback_hours)
            ticks = mt5.copy_ticks_range(symbol, from_dt, to_dt, mt5.COPY_TICKS_ALL)
            if ticks is None or len(ticks) == 0:
                return []
            out = [{"time": self.to_utc(int(t["time"])), "bid": float(t["bid"]), "ask": float(t["ask"])}
                   for t in ticks[-count:]]
            return out
        # demo: walk the last M1 closes, 4 synthetic ticks per bar
        df = self.get_m1(symbol, 120)
        out = []
        for _, r in df.iterrows():
            base = int(r["time"])
            c = float(r["close"])
            for k in range(4):
                out.append({"time": base + k * 15, "bid": round(c, 3),
                            "ask": round(c + 0.15, 3), "synthetic": True})
        return out[-count:]

    def get_ticks_range(self, symbol, start_ts, end_ts):
        """Historical ticks for [start_ts, end_ts) as [(ms, bid, ask)].

        feeds the tick-history gap fill - ranges MT5's rate history
        won't serve can still be rebuilt from real ticks (copy_ticks_range
        reaches back many days on most brokers). Live only; demo mode
        returns [] so synthetic ticks can never heal real gaps.
        """
        if not self.live:
            return []
        try:
            sym = self.resolve_symbol(symbol)[0]
            from_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            to_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
            ticks = mt5.copy_ticks_range(sym, from_dt, to_dt,
                                         mt5.COPY_TICKS_ALL)
        except Exception:
            return []
        if ticks is None:
            return []
        out = []
        for t in ticks:
            try:
                out.append((int(t["time_msc"]), float(t["bid"]),
                            float(t["ask"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def get_quote(self, symbol):
        """Latest bid/ask + time."""
        if self.live:
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return None
            return {"bid": float(t.bid), "ask": float(t.ask), "time": self.to_utc(int(t.time))}
        df = self.get_m1(symbol, 1)
        c = float(df.iloc[-1]["close"])
        return {"bid": c, "ask": round(c + 0.15, 3), "time": int(df.iloc[-1]["time"]),
                "synthetic": True}

    def server_time(self):
        if self.live:
            return int(time.time())
        df = self.get_m1("XAUUSD", 1)
        return int(df.iloc[-1]["time"])

    # ------------------------------------------------ live bar sync
    # The chart must show EXACTLY what MT5 shows. The old client guessed the
    # forming bar's OHLC from 1-second quote polls (missing inter-poll ticks,
    # out-of-order responses, fabricated rollover opens). Now the server -
    # which talks to MT5 directly - is the single source of truth: every
    # quote poll also returns MT5's own forming bar for the pane's timeframe,
    # and the client renders exactly that, never inventing OHLC itself.
    _LIVE_M1_MULT = {"1m": 1, "3m": 3, "5m": 5}

    def next_seq(self, symbol):
        """Monotonic per-symbol quote sequence; the client drops any response
        whose seq is not newer than the last one it applied (kills
        out-of-order poll overwrites)."""
        self._qseq[symbol] = self._qseq.get(symbol, 0) + 1
        return self._qseq[symbol]

    @staticmethod
    def _bar_dict(t, o, h, l, c, v=0):
        # MT5 tick volume rides along when the caller has it.
        return {"time": int(t), "open": round(float(o), 3),
                "high": round(float(h), 3), "low": round(float(l), 3),
                "close": round(float(c), 3), "volume": int(v or 0)}

    def get_live_bar(self, symbol, tf):
        """Authoritative forming + previous-closed bar for `tf`, per MT5.

        Returns {"forming": bar|None, "prev_closed": bar|None}.
        Live: read straight from the terminal (M1 buckets for 1m/3m/5m,
        native MT5 timeframe above that, real tick buckets for 1s/15s).
        Demo: tail of the bundled history (static, but structurally identical).
        """
        try:
            if tf in self._LIVE_M1_MULT:
                return self._live_bar_m1(symbol, self._LIVE_M1_MULT[tf])
            if tf in self._NATIVE_TF:
                return self._live_bar_native(symbol, tf)
            if tf in ("1s", "15s"):
                return self._live_bar_tick(symbol, 1 if tf == "1s" else 15)
        except Exception:
            pass
        return {"forming": None, "prev_closed": None}

    def _live_bar_m1(self, symbol, mult):
        if self.live:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, mult + 1)
            if rates is None or len(rates) == 0:
                return {"forming": None, "prev_closed": None}
            rows = [{"time": self.to_utc(int(r["time"])), "open": float(r["open"]),
                     "high": float(r["high"]), "low": float(r["low"]),
                     "close": float(r["close"]),
                     "volume": int(r["tick_volume"])} for r in rates]
        else:
            df = self._demo_m1(mult + 1)
            rows = [{"time": int(r.time), "open": float(r.open),
                     "high": float(r.high), "low": float(r.low),
                     "close": float(r.close)} for r in df.itertuples()]
            if not rows:
                return {"forming": None, "prev_closed": None}
        if mult == 1:
            forming = rows[-1]
            prev = rows[-2] if len(rows) > 1 else None
        else:
            sec = mult * 60
            groups = {}
            for r in rows:
                g = (r["time"] // sec) * sec
                groups.setdefault(g, []).append(r)
            keys = sorted(groups)
            # rebuild properly with group keys (keep it explicit & safe)
            def _agg(gkey, rs):
                return self._bar_dict(gkey, rs[0]["open"],
                                      max(x["high"] for x in rs),
                                      min(x["low"] for x in rs),
                                      rs[-1]["close"],
                                      sum(x.get("volume", 0) for x in rs))
            forming = _agg(keys[-1], groups[keys[-1]])
            prev = _agg(keys[-2], groups[keys[-2]]) if len(keys) > 1 else None
        return {"forming": forming, "prev_closed": prev}

    def _live_bar_native(self, symbol, tf):
        if self.live:
            tf_const = getattr(mt5, "TIMEFRAME_" + self._NATIVE_TF[tf])
            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, 2)
            if rates is None or len(rates) == 0:
                return {"forming": None, "prev_closed": None}
            rows = [self._bar_dict(self.to_utc(r["time"]), r["open"], r["high"],
                                   r["low"], r["close"], r["tick_volume"])
                    for r in rates]
        else:
            df = self._demo_resample(tf, 2)
            rows = [self._bar_dict(r.time, r.open, r.high, r.low, r.close)
                    for r in df.itertuples()]
            if not rows:
                return {"forming": None, "prev_closed": None}
        return {"forming": rows[-1],
                "prev_closed": rows[-2] if len(rows) > 1 else None}

    def _live_bar_tick(self, symbol, sec):
        """Forming 1s/15s bar from real tick buckets (live only; demo has M1)."""
        if not self.live:
            return {"forming": None, "prev_closed": None}
        now = datetime.now(timezone.utc)
        now_s = int(now.timestamp())
        b0 = (now_s // sec) * sec
        ticks = mt5.copy_ticks_range(
            symbol,
            datetime.fromtimestamp(b0, tz=timezone.utc),
            now + timedelta(seconds=1), mt5.COPY_TICKS_ALL) or []
        forming = None
        if ticks:
            px = [float(t["bid"]) for t in ticks]
            forming = self._bar_dict(b0, px[0], max(px), min(px), px[-1],
                                     len(ticks))
        prev = None
        pticks = mt5.copy_ticks_range(
            symbol,
            datetime.fromtimestamp(b0 - 2 * sec, tz=timezone.utc),
            datetime.fromtimestamp(b0, tz=timezone.utc),
            mt5.COPY_TICKS_ALL) or []
        if pticks:
            buckets = {}
            for t in pticks:
                bt = (self.to_utc(int(t["time"])) // sec) * sec
                buckets.setdefault(bt, []).append(float(t["bid"]))
            done = sorted(k for k in buckets if k < b0)
            if done:
                px = buckets[done[-1]]
                # bucket keys are server-basis tick times; b0 is UTC.
                # Whole-hour broker offsets never move a 1s/15s boundary,
                # but the bar's label must still be true UTC.
                prev = self._bar_dict(self.to_utc(done[-1]), px[0], max(px), min(px),
                                     px[-1], len(px))
        if forming is None and prev is not None:
            # quiet bucket so far: flat bar seeded from previous close
            forming = self._bar_dict(b0, prev["close"], prev["close"],
                                     prev["close"], prev["close"])
        return {"forming": forming, "prev_closed": prev}

    # the per-second Renko tracker is gone. The shared RenkoChain
    # owns the live edge (vault 1-second bars), so /api/quote's renko branch
    # reads the same bricks as the chart, indicators and the Alert Engine -
    # one series, no second anchor that could diverge and blink.

    # --------------------------------------------------------------- watchlist
    def _prev_close(self, symbol):
        """Previous daily close (for Chg/Chg%), cached per UTC date."""
        today = datetime.now(timezone.utc).date().isoformat()
        hit = self._prev_close_cache.get(symbol)
        if hit and hit[0] == today:
            return hit[1]
        prev = 0.0
        try:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 1)
            if rates is not None and len(rates):
                prev = float(rates[0]["close"])
        except Exception:
            pass
        self._prev_close_cache[symbol] = (today, prev)
        return prev

    def prev_close(self, symbol):
        """Public wrapper for the previous daily close (0.0 = unknown)."""
        try:
            return self._prev_close(symbol)
        except Exception:
            return 0.0

    def get_watchlist(self):
        """Market Watch symbols with live quotes + day change.

        MT5 only streams quotes for symbols that are IN its Market Watch, so
        this lists exactly those (symbol_info.select == True). Adding a symbol
        here calls symbol_select(sym, True), which puts it into MT5's Market
        Watch as well - that step is mandatory, there is no way to quote a
        symbol without it."""
        if not self.live:
            return self._demo_watchlist()
        out = []
        try:
            symbols = mt5.symbols_get() or []
        except Exception:
            return out
        for s in symbols:
            try:
                if not getattr(s, "select", False):
                    continue
                name, digits = s.name, int(s.digits)
            except Exception:
                continue
            # a selected symbol with no tick yet (market closed for it,
            # weekend, tick not received) must STILL be listed - the app
            # watchlist has to match MT5's Market Watch exactly. Quotes show
            # as unavailable instead of the row being silently dropped.
            bid = ask = None
            try:
                tick = mt5.symbol_info_tick(name)
            except Exception:
                tick = None
            if tick is not None and tick.bid:
                bid = float(tick.bid)
                ask = float(tick.ask) if tick.ask else None
            prev = self._prev_close(name)
            chg = (bid - prev) if (bid is not None and prev) else 0.0
            out.append({
                "symbol": name,
                "bid": round(bid, digits) if bid is not None else None,
                "ask": round(ask, digits) if ask is not None else None,
                "change": round(chg, digits),
                "change_pct": round(chg / prev * 100, 2) if (bid is not None and prev) else 0.0,
                "digits": digits,
                "no_quote": bid is None,
            })
            if len(out) >= 120:
                break
        return out

    def watch_add(self, symbol):
        """Add to MT5 Market Watch (mandatory before quotes flow). Idempotent."""
        if not self.live:
            raise RuntimeError("MT5 is not connected")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 refused to add {symbol}: {mt5.last_error()}")
        return True

    def watch_remove(self, symbol):
        if not self.live:
            raise RuntimeError("MT5 is not connected")
        if not mt5.symbol_select(symbol, False):
            raise RuntimeError(
                f"MT5 refused to remove {symbol} "
                f"(a chart is open or it has positions?): {mt5.last_error()}")
        return True

    def sync_watchlist(self, keep=None, progress=None):
        """Reconcile with MT5's Market Watch - MT5 is the source of truth.

        keep: None = plain re-read; a list prunes every other selected symbol
        from Market Watch (an EMPTY list prunes everything - only ever pass
        that after an explicit user confirmation). Removals go through
        watch_remove, so MT5 refusals surface as errors instead of silent
        skips. progress: optional callable(stage_text) for live UI narration.
        Returns {"symbols": [...], "removed": [...], "errors": [...]}.
        keep=[] prunes all; progress callback +
        bounded MT5 calls (a dead terminal degrades to honest timeout
        errors instead of wedging the sync forever)."""
        def _prog(text):
            try:
                if progress is not None:
                    progress(text)
            except Exception:
                pass

        if not self.live or mt5 is None:
            raise RuntimeError("MT5 is not connected")

        def selected():
            _prog("Reading MT5 Market Watch...")
            try:
                syms = _bounded_call(lambda: mt5.symbols_get(), 30,
                                     "Reading MT5 Market Watch") or []
                return [x for x in syms if getattr(x, "select", False)]
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError("MT5 symbols_get failed: %s" % e)

        removed, errors = [], []
        if keep is not None:  # [] prunes EVERYTHING (explicit user confirm)
            keep_set = set(keep)
            victims = [x for x in selected() if x.name not in keep_set]
            for i, x in enumerate(victims, 1):
                _prog("Removing %s (%d/%d)..." % (x.name, i, len(victims)))
                try:
                    _bounded_call(lambda n=x.name: self.watch_remove(n), 20,
                                  "Removing %s from Market Watch" % x.name)
                    removed.append(x.name)
                except RuntimeError as e:
                    errors.append(str(e))
        _prog("Reading final Market Watch list...")
        symbols = sorted(x.name for x in selected())
        _prog("Done")
        return {"symbols": symbols, "removed": removed, "errors": errors}

    def search_symbols(self, q):
        """Search all broker symbols (for the watchlist add dialog)."""
        if not self.live:
            return []
        q = (q or "").strip()
        try:
            res = mt5.symbols_get("*" + q + "*" if q else "*") or []
        except Exception:
            return []
        return [s.name for s in res[:100]]

    def _demo_watchlist(self):
        try:
            df = self._demo_m1(3000)
        except Exception:
            return []
        last = float(df.iloc[-1]["close"])
        d = df.copy()
        d["day"] = (pd.to_datetime(d["time"], unit="s", utc=True)
                      .dt.tz_convert("Asia/Kolkata").dt.date.astype(str))
        days = d["day"].unique()
        prev = float(d[d["day"] == days[-2]]["close"].iloc[-1]) if len(days) > 1 else last
        chg = last - prev
        return [{"symbol": "XAUUSD", "bid": round(last, 2),
                 "ask": round(last + 0.15, 2),
                 "change": round(chg, 2),
                 "change_pct": round(chg / prev * 100, 2) if prev else 0.0,
                 "digits": 2, "synthetic": True}]
