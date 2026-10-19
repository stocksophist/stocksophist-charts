"""
OANDA v20 data feed for StockSophist Charts.

Implements the SAME public interface as mt5feed.Feed so server.py can swap
the feed object based on settings["data_source"] ("mt5" | "oanda") with no
other changes:

  - History: OANDA REST /v3/instruments/{XAU_USD}/candles
              (M1 back to ~2005-era depth, 5000 candles per request,
              paginated). Returned as DataFrames with the exact schema the
              app expects: time, open, high, low, close, volume
              (unix-seconds, true UTC - OANDA needs no server-offset math).
  - Live: OANDA streaming /v3/accounts/{id}/pricing/stream pushes
              every bid/ask price change tick-by-tick. A background thread
              keeps the newest tick in memory; get_quote() serves it, so the
              existing 1-second tick-recorder loop, tick vault, live candles,
              indicators, Renko and alerts all work UNCHANGED.
  - Symbol boundary: the app keeps saying "XAUUSD" everywhere (caches,
              vault, UI); only the wire calls use OANDA's "XAU_USD".

Honest limits (never fabricated):
  - OANDA's REST API has NO historical tick endpoint, so get_ticks_range()
    returns [] (like MT5 demo mode does). The tick vault fills forward from
    the live stream instead.
  - 1s/15s panes can only show what the vault has recorded since the stream
    started (or from MT5 history if the user switches back).

Auth: settings.json -> {"oanda": {"token": "...", "account_id": "..."}} or
env OANDA_TOKEN / OANDA_ACCOUNT_ID. The token is NEVER baked into a release
zip - it lives only in the user's local settings.json.
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import pandas as pd

REST_HOST = "https://api-fxpractice.oanda.com"
STREAM_HOST = "https://stream-fxpractice.oanda.com"

# Outward (app) symbol -> OANDA instrument. Gold has explicit variants; other
# symbols are normalised at the API boundary: broker suffixes (#, .) are
# stripped and 6-letter spot pairs are underscored (EURUSD -> EUR_USD) so
# watch_add(), candle history and the stream all use OANDA's names while the
# app/UI keeps displaying clean symbols (EURUSD, XAUUSD).
_GOLD = "XAU_USD"

# chart tf -> OANDA granularity
_TF_GRAN = {
    "1m": "M1", "3m": "M1", "5m": "M1",          # M1 multiples resample locally
    "15m": "M15", "30m": "M30",
    "1H": "H1", "4H": "H4", "D": "D", "W": "W", "M": "M",
}
_M1_MULT = {"1m": 1, "3m": 3, "5m": 5}

_MAX_PER_REQUEST = 5000   # OANDA candle page size
_STREAM_TIMEOUT = 30      # seconds; heartbeats arrive ~every 10s
_QUOTE_STALE_S = 45       # stream tick older than this -> REST fallback
_LIVE_TTL = 10            # liveness cache, mirrors mt5feed


def _to_inst(symbol):
    s = (symbol or "").strip().upper().replace("/", "_")
    if s in ("XAUUSD", "GOLD", "GOLD#", "XAUUSD.", "XAUUSD#", "XAUUSDM"):
        return _GOLD
    # broker suffix, e.g. "EURUSD#" -> "EURUSD"
    if s.endswith("#") or s.endswith("."):
        s = s[:-1]
    # OANDA uses "EUR_USD" style instrument names
    if "_" not in s and len(s) == 6 and s.isalpha():
        s = s[:3] + "_" + s[3:]
    return s


def _rfc3339(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s):
    # "T12:34:00.000000000Z" -> unix seconds
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        frac = 0.0
        rest = s[19:].rstrip("Z")
        if rest.startswith("."):
            frac = float("0" + rest)
        return int(dt.replace(tzinfo=timezone.utc).timestamp() + frac)
    except Exception:
        return 0


_SETTINGS_PATH_OVERRIDE = None


def set_settings_path(path):
    """server.py points the feed at the migrated data-dir settings
    file before creating it. When unset, the legacy in-tree lookup applies."""
    global _SETTINGS_PATH_OVERRIDE
    _SETTINGS_PATH_OVERRIDE = path


class OandaFeed:
    tz_label = "OANDA practice (UTC)"

    def __init__(self, token=None, account_id=None):
        s = {}
        _extra = []  # persisted watchlist additions
        try:
            # server.py sets the migrated (data-dir) settings path
            # before creating the feed; fall back to the legacy in-tree copy.
            _sp = _SETTINGS_PATH_OVERRIDE or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "settings.json")
            with open(_sp) as f:
                _full = json.load(f) or {}
            s = _full.get("oanda") or {}
            _extra = _full.get("oanda_extra_symbols") or []
        except Exception:
            pass
        self.token = (token or os.environ.get("OANDA_TOKEN") or
                      s.get("token") or "").strip()
        self.account_id = (account_id or os.environ.get("OANDA_ACCOUNT_ID") or
                           s.get("account_id") or "").strip()
        self._live_cache = None
        self._live_at = 0.0
        self._last_tick = None   # {"bid","ask","time"} from the stream
        self._tick_at = 0.0      # wall clock of last stream tick
        self._last_beat = 0.0    # wall clock of last heartbeat
        self._stream_up = False
        self._qseq = {}
        self._extra_symbols = {str(x).strip().upper() for x in _extra
                               if isinstance(x, str) and str(x).strip()}
        # watchlist additions persist in settings.json
        # (oanda_extra_symbols) - persisted across sessions.
        self._inst_cache = set()
        self._inst_at = 0.0
        self._hist_fail = 0
        self._cb_state = "closed"
        self._cb_opened_at = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        if self.token and self.account_id:
            t = threading.Thread(target=self._stream_loop, daemon=True)
            t.start()

    # ----------------------------------------------------------- liveness
    def _rest(self, path, params=None, timeout=20, stream=False):
        url = REST_HOST + path
        if params:
            url += "?" + "&".join("%s=%s" % (k, v) for k, v in params.items())
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self.token,
                          "Accept-Datetime-Format": "RFC3339"})
        return urllib.request.urlopen(req, timeout=timeout)

    def _probe_live(self):
        if not (self.token and self.account_id):
            return False
        try:
            with self._rest("/v3/accounts/%s/pricing" % self.account_id,
                            {"instruments": _GOLD}, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return bool(data.get("prices"))
        except Exception:
            return False

    @property
    def live(self):
        now = time.monotonic()
        if self._live_cache is not None and now - self._live_at < _LIVE_TTL:
            return self._live_cache
        v = self._probe_live()
        self._live_cache = v
        self._live_at = now
        return v

    def live_fresh(self):
        v = self._probe_live()
        self._live_cache = v
        self._live_at = time.monotonic()
        return v

    @property
    def stream_alive(self):
        """The tick stream pushed something in the last 60s."""
        return (time.time() - max(self._tick_at, self._last_beat)) < 60

    # ------------------------------------------------------ history health
    def note_hist_ok(self):
        self._hist_fail = 0
        if self._cb_state == "half-open":
            self._cb_state = "closed"

    def note_hist_fail(self):
        self._hist_fail += 1
        if self._hist_fail >= 5 and self._cb_state == "closed":
            self._cb_state = "open"
            self._cb_opened_at = time.time()

    def hist_mt5_allowed(self):
        # Same contract as mt5feed (name kept so server.py is untouched):
        # whether the history path may be hit right now.
        if self._cb_state == "closed":
            return True
        if self._cb_state == "open":
            if time.time() - self._cb_opened_at > 120:
                self._cb_state = "half-open"
                return True
            return False
        return True  # half-open: one probe allowed

    @property
    def hist_circuit(self):
        # MUST be a property - server.py serializes it straight
        # into /api/status and /api/startup_status via jsonify. As a plain
        # method it raised "Object of type method is not JSON serializable"
        # > HTTP 500 on both endpoints in OANDA mode (DEMO badge stuck,
        # sync overlay never completing).
        return self._cb_state

    @property
    def degraded(self):
        return self._cb_state != "closed" or not self.stream_alive

    # ------------------------------------------------------------- symbols
    def detect_symbol(self):
        return "XAUUSD"

    @property
    def symbol(self):
        return "XAUUSD"

    def resolve_symbol(self, requested):
        s = (requested or "").strip() or "XAUUSD"
        return s, None

    def search_symbols(self, q):
        q = (q or "").strip().upper()
        base = ["XAUUSD", "BTCUSD", "ETHUSD", "EURUSD", "GBPUSD", "USDJPY",
                "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURJPY",
                "XAGUSD"]
        # search the account's LIVE OANDA instrument list so ANY
        # listed instrument is addable - with the hardcoded list, BTC was
        # un-addable because no query matched it. Display form strips the
        # underscore: BTC_USD -> BTCUSD (watch_add maps it back via
        # _to_inst). Empty instrument list (fetch failed) falls back to
        # the hardcoded base list.
        try:
            insts = self._instruments()
        except Exception:
            insts = None
        if insts:
            names = sorted(i.replace("_", "") for i in insts if i)
            return [s for s in names if q in s][:100]
        return [s for s in base if q in s][:100]

    # ------------------------------------------------ MT5-only operations
    # Watchlist sync / symbol add-remove are MT5-terminal concepts. In OANDA
    # mode they refuse with an honest message instead of AttributeError.
    def sync_watchlist(self, keep=None, progress=None):
        raise RuntimeError(
            "Watchlist sync manages the MT5 terminal's Market Watch - "
            "switch the data feed back to MT5 to use it.")

    def watch_remove(self, symbol):
        # OANDA additions CAN be removed - they live in our own
        # _extra_symbols, not the MT5 terminal's Market Watch.
        s = (symbol or "").strip().upper()
        if s in self._extra_symbols:
            self._extra_symbols.discard(s)
            return True
        raise RuntimeError(
            "Removing symbols manages the MT5 terminal's Market Watch - "
            "switch the data feed back to MT5 to use it.")

    # ------------------------------------------------------ watchlist-ish
    def prev_close(self, symbol):
        """Previous daily close (0.0 = unknown)."""
        try:
            df = self._candles(symbol, "D", count=2)
            if df is not None and len(df) >= 2:
                return float(df["close"].iloc[-2])
            if df is not None and len(df) == 1:
                return float(df["close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    def get_watchlist(self):
        """OANDA has no Market Watch - the app watchlist is just the symbols
        the user charts (XAUUSD plus anything added this session)."""
        syms = ["XAUUSD"] + sorted(self._extra_symbols)
        out = []
        for s in syms:
            q = None
            try:
                q = self.get_quote(s)
            except Exception:
                pass
            bid = q.get("bid") if q else None
            ask = q.get("ask") if q else None
            prev = self.prev_close(s)
            chg = (bid - prev) if (bid is not None and prev) else 0.0
            out.append({
                "symbol": s,
                "bid": round(bid, 2) if bid is not None else None,
                "ask": round(ask, 2) if ask is not None else None,
                "change": round(chg, 2),
                "change_pct": round(chg / prev * 100, 2) if (bid is not None and prev) else 0.0,
                "digits": 2,
                "no_quote": bid is None,
            })
        return out

    def extra_symbols(self):
        """persisted watchlist additions (saved in settings.json
        under oanda_extra_symbols). The server persists them after every
        successful watch_add / watch_remove."""
        return sorted(self._extra_symbols)

    def watch_add(self, symbol):
        """Accept any symbol OANDA lists (checked against the account's
        instrument list); anything else is refused honestly."""
        s = (symbol or "").strip().upper()
        if not s:
            raise RuntimeError("symbol required")
        inst = _to_inst(s)
        if inst == _GOLD or s in self._extra_symbols:
            self._extra_symbols.add(s)
            return True
        if inst in self._instruments():
            self._extra_symbols.add(s)
            return True
        raise RuntimeError("OANDA has no instrument for %s" % s)

    def _instruments(self):
        """Cached set of tradeable OANDA instrument names."""
        now = time.time()
        if self._inst_cache and now - self._inst_at < 3600:
            return self._inst_cache
        out = set()
        try:
            with self._rest("/v3/accounts/%s/instruments" % self.account_id,
                            timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for it in data.get("instruments") or []:
                if it.get("name"):
                    out.add(it["name"])
        except Exception:
            pass
        if out:
            self._inst_cache, self._inst_at = out, now
        return out or self._inst_cache

    # ------------------------------------------------------ candle history
    def _candles(self, symbol, granularity, count=None, start_ts=None,
                 end_ts=None):
        """Raw OANDA candles -> DataFrame(time,open,high,low,close,volume).

        Bid-based OHLC (matches MT5's bid convention); volume is OANDA's
        tick count for the candle. Times are true UTC unix seconds."""
        inst = _to_inst(symbol)
        params = {"price": "B", "granularity": granularity}
        pages = []
        if start_ts is not None and end_ts is not None:
            # paginate backward: OANDA takes from+to (or count); walk `to`
            # backward until we cover start_ts or the feed exhausts.
            to_ts = int(end_ts)
            guard = 0
            while to_ts > int(start_ts) and guard < 4000:
                guard += 1
                params.update({"from": _rfc3339(max(int(start_ts), to_ts - _MAX_PER_REQUEST * _gran_sec(granularity))),
                               "to": _rfc3339(to_ts)})
                data = self._get_json("/v3/instruments/%s/candles" % inst, params)
                cs = data.get("candles") or []
                if not cs:
                    break
                pages.append(cs)
                oldest = _parse_time(cs[0]["time"])
                if oldest <= int(start_ts) or len(cs) < 2:
                    break
                to_ts = oldest  # exclusive-ish; overlap trimmed at merge
            candles = [c for page in reversed(pages) for c in page]
        else:
            params["count"] = min(int(count or 500), _MAX_PER_REQUEST)
            data = self._get_json("/v3/instruments/%s/candles" % inst, params)
            candles = data.get("candles") or []
        rows = []
        for c in candles:
            try:
                b = c.get("bid") or {}
                if b.get("o") is None:
                    continue  # no bid quote in this bucket (illiquid minute)
                rows.append({"time": _parse_time(c["time"]),
                             "open": float(b["o"]), "high": float(b["h"]),
                             "low": float(b["l"]), "close": float(b["c"]),
                             "volume": int(c.get("volume") or 0)})
            except (TypeError, ValueError):
                continue
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low",
                                         "close", "volume"])
        if len(df):
            df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
            if start_ts is not None:
                df = df[(df["time"] >= int(start_ts)) & (df["time"] <= int(end_ts))]
                df = df.reset_index(drop=True)
        return df

    def _get_json(self, path, params):
        try:
            with self._rest(path, params) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(
                    "OANDA rejected the request (HTTP %s) - check the API "
                    "token and account ID in the Data dialog." % e.code)
            raise RuntimeError("OANDA history failed (HTTP %s)" % e.code)
        except Exception as e:
            raise RuntimeError("OANDA history failed: %s" % e)

    @staticmethod
    def _need_live():
        raise RuntimeError("OANDA is not connected - add the API token and "
                           "account ID in the Data dialog.")

    def get_m1(self, symbol, count=3000):
        if not self.live:
            self._need_live()
        frames, to_ts, got = [], None, 0
        # Newest page first (count=), then older pages via from/to.
        while got < count:
            n = min(_MAX_PER_REQUEST, count - got)
            if to_ts:
                df = self._candles(symbol, "M1",
                                   start_ts=to_ts - n * 60, end_ts=to_ts)
            else:
                df = self._candles(symbol, "M1", count=n)
            if df is None or len(df) == 0:
                break
            frames.insert(0, df)
            got += len(df)
            if len(df) < n:
                break  # feed exhausted - honest short history
            to_ts = int(df["time"].iloc[0])
        if not frames:
            raise RuntimeError("OANDA returned no M1 data for %s" % symbol)
        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        return out.tail(count).reset_index(drop=True)

    def get_m1_deep(self, symbol, target=20000):
        return self.get_m1(symbol, count=target)

    def get_rates(self, symbol, tf_name, count=600):
        gran = _TF_GRAN.get(tf_name)
        if gran is None:
            raise ValueError("unknown tf %s" % tf_name)
        if gran == "M1":
            df = self.get_m1(symbol, count * _M1_MULT.get(tf_name, 1) + 1)
            return self.resample(df, _M1_MULT.get(tf_name, 1)).tail(count).reset_index(drop=True)
        if not self.live:
            self._need_live()
        return self._candles(symbol, gran, count=count).tail(count).reset_index(drop=True)

    def get_rates_deep(self, symbol, tf_name, target=20000):
        gran = _TF_GRAN.get(tf_name)
        if gran is None:
            raise ValueError("unknown tf %s" % tf_name)
        if gran == "M1":
            df = self.get_m1(symbol, target * _M1_MULT.get(tf_name, 1) + 1)
            return self.resample(df, _M1_MULT.get(tf_name, 1)).tail(target).reset_index(drop=True)
        if not self.live:
            self._need_live()
        return self._candles(symbol, gran, count=target).tail(target).reset_index(drop=True)

    def get_rates_range(self, symbol, tf_name, start_ts, end_ts):
        gran = _TF_GRAN.get(tf_name)
        if gran is None:
            raise ValueError("unknown tf %s" % tf_name)
        if not self.live:
            self._need_live()
        if gran == "M1":
            mult = _M1_MULT.get(tf_name, 1)
            df = self._candles(symbol, "M1", start_ts=int(start_ts) - mult * 60,
                               end_ts=int(end_ts))
            return self.resample(df, mult)
        return self._candles(symbol, gran, start_ts=int(start_ts),
                             end_ts=int(end_ts))

    @staticmethod
    def resample(df, tf_min):
        """M1 -> N-minute bars (same contract as mt5feed.Feed.resample)."""
        if df is None or len(df) == 0 or tf_min <= 1:
            return df
        sec = int(tf_min) * 60
        d = df.copy()
        d["grp"] = (d["time"] // sec) * sec
        agg = d.groupby("grp").agg(
            time=("grp", "first"), open=("open", "first"),
            high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"))
        return agg.reset_index(drop=True)[["time", "open", "high", "low",
                                           "close", "volume"]]

    # --------------------------------------------------------------- ticks
    def get_ticks(self, symbol, count=2000, lookback_hours=6):
        # OANDA has no historical tick endpoint - the vault fills forward
        # from the live stream. Honest [] (same as MT5 demo mode).
        return []

    def get_ticks_range(self, symbol, start_ts, end_ts):
        return []

    def get_tickbars(self, symbol, sec, n_bars=400):
        # sub-minute history from OANDA's native second-level
        # candles (S5/S10/S15/S30) instead of raising. Same (bars, quote)
        # tuple contract as the MT5 feed. OANDA caps a candle request at
        # _MAX_PER_REQUEST (5000), so a 5760-bar 15s ask returns ~20.8h -
        # enough to seed Renko bricks; the tail keeps filling live.
        gran = {5: "S5", 10: "S10", 15: "S15", 30: "S30"}.get(sec)
        if gran is None:
            raise RuntimeError(
                "OANDA has no %ds candles (finest is 5s) - "
                "1s Renko history is MT5-only." % sec)
        if not self.live:
            self._need_live()
        df = self._candles(symbol, gran, count=min(int(n_bars),
                                                   _MAX_PER_REQUEST))
        # Match the MT5 tickbar dict format; Flask must JSON-serialize
        # these, so use Python natives, not numpy types.
        bars = [{"time": int(r["time"]), "open": float(r["open"]),
                 "high": float(r["high"]), "low": float(r["low"]),
                 "close": float(r["close"]), "volume": int(r["volume"])}
                for r in df.to_dict("records")]
        return bars, self.get_quote(symbol)

    # ---------------------------------------------------------- live quote
    def get_quote(self, symbol):
        """Newest streamed tick; REST pricing fallback if the stream is stale."""
        with self._lock:
            tick = self._last_tick
            age = time.time() - self._tick_at
        if tick and age < _QUOTE_STALE_S:
            return dict(tick)
        # stream stale/dead - one-shot REST pricing (still real OANDA data)
        if self.token and self.account_id:
            try:
                with self._rest("/v3/accounts/%s/pricing" % self.account_id,
                                {"instruments": _to_inst(symbol)},
                                timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                px = (data.get("prices") or [None])[0]
                if px and px.get("bids") and px.get("asks"):
                    return {"bid": float(px["bids"][0]["price"]),
                            "ask": float(px["asks"][0]["price"]),
                            "time": _parse_time(px["time"])}
            except Exception:
                pass
        return dict(tick) if tick else None

    def get_live_bar(self, symbol, tf):
        """Authoritative forming + previous-closed bar, per OANDA.

        Same contract as mt5feed: {"forming": bar|None, "prev_closed": ...}.
        For M1 multiples we pull a few M1 candles and aggregate locally -
        OANDA's own candles, never invented. 1s/15s: the vault owns those."""
        try:
            if tf in ("1m", "3m", "5m"):
                mult = _M1_MULT[tf]
                df = self.get_m1(symbol, mult + 1)
                if df is None or len(df) == 0:
                    return {"forming": None, "prev_closed": None}
                if mult == 1:
                    rows = [_bar_dict(r) for r in df.itertuples()]
                    return {"forming": rows[-1],
                            "prev_closed": rows[-2] if len(rows) > 1 else None}
                rs = self.resample(df, mult)
                rows = [_bar_dict(r) for r in rs.itertuples()]
                return {"forming": rows[-1],
                        "prev_closed": rows[-2] if len(rows) > 1 else None}
            if tf in _TF_GRAN and _TF_GRAN[tf] != "M1":
                df = self.get_rates(symbol, tf, 2)
                rows = [_bar_dict(r) for r in df.itertuples()]
                if not rows:
                    return {"forming": None, "prev_closed": None}
                return {"forming": rows[-1],
                        "prev_closed": rows[-2] if len(rows) > 1 else None}
        except Exception:
            pass
        return {"forming": None, "prev_closed": None}

    # ----------------------------------------------------------------- time
    def utc_offset(self):
        return 0  # OANDA speaks true UTC

    def to_utc(self, ts):
        return int(ts)

    def resync_time(self):
        return 0

    def server_time(self):
        return int(time.time())

    def next_seq(self, symbol):
        self._qseq[symbol] = self._qseq.get(symbol, 0) + 1
        return self._qseq[symbol]

    # --------------------------------------------------------- price stream
    def _stream_loop(self):
        """Tick-by-tick price stream with reconnect backoff. Daemon thread."""
        url = (STREAM_HOST + "/v3/accounts/%s/pricing/stream?instruments=%s"
               % (self.account_id, _GOLD))
        backoff = 5
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + self.token,
                                  "Accept-Datetime-Format": "RFC3339"})
                with urllib.request.urlopen(req, timeout=_STREAM_TIMEOUT + 60) as r:
                    self._stream_up = True
                    backoff = 5
                    print("[oanda] price stream connected", flush=True)
                    buf = b""
                    while not self._stop.is_set():
                        chunk = r.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._handle_stream_line(line)
            except Exception as e:
                # log stream failures - silent reconnects made
                # OANDA outages invisible in the server console.
                print("[oanda] price stream error: %s (retry in %ss)"
                      % (e, min(backoff, 60)), flush=True)
                self._stream_up = False
            # reconnect with backoff (5s .. 60s)
            self._stop.wait(min(backoff, 60))
            backoff = min(backoff * 2, 60)

    def _handle_stream_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return
        t = msg.get("type")
        if t == "HEARTBEAT":
            self._last_beat = time.time()
            return
        if t != "PRICE":
            return
        try:
            bids = msg.get("bids") or []
            asks = msg.get("asks") or []
            if not bids or not asks:
                return
            tick = {"bid": float(bids[0]["price"]),
                    "ask": float(asks[0]["price"]),
                    "time": _parse_time(msg.get("time") or "")}
            if tick["time"] <= 0:
                tick["time"] = int(time.time())
            with self._lock:
                self._last_tick = tick
                self._tick_at = time.time()
        except (TypeError, ValueError):
            pass

    def close(self):
        self._stop.set()


def _gran_sec(gran):
    return {"S5": 5, "S10": 10, "S15": 15, "S30": 30, "M1": 60, "M2": 120,
            "M4": 240, "M5": 300, "M10": 600, "M15": 900, "M30": 1800,
            "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
            "H8": 28800, "H12": 43200, "D": 86400, "W": 604800,
            "M": 2592000}.get(gran, 60)


def _bar_dict(r):
    return {"time": int(r.time), "open": round(float(r.open), 3),
            "high": round(float(r.high), 3), "low": round(float(r.low), 3),
            "close": round(float(r.close), 3),
            "volume": int(getattr(r, "volume", 0) or 0)}
