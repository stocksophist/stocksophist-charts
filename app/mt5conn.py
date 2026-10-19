"""
MT5 connection manager - the robust way to attach Python to the terminal.

Research-backed design (MetaTrader5 package docs + production watchdog patterns):

  1. initialize() attaches to an already-running terminal, or launches the
     terminal at `path` when it isn't running. Called with no args it uses the
     last account; with login/password/server it selects the account.
  2. login() switches the trading account on an already-established terminal
     connection - the cheap repair path (no terminal restart).
  3. Health = terminal_info() exists AND terminal_info().connected AND
     account_info() is not None. A background watchdog re-checks this and
     repairs (re-login, then re-initialize) instead of dying silently.

Multiple Python processes (this chart app + the trading engine) can attach to
the SAME running terminal concurrently - no conflict.

Config lives in mt5_connection.json next to the app (created on first save,
never shipped in the zip). The password is stored in that local file on the
user's own PC - same trade-off as the trading engine's settings.json.
"""

import glob
import json
import os
import threading
import time

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False


DEFAULTS = {
    "path": "",          # terminal64.exe location; "" = auto-detect / attach to running
    "login": "",         # account number
    "password": "",      # account password
    "server": "",        # trade server, e.g. "Exness-MT5Real"
    "timeout": 60000,    # ms
    "portable": False,
    "remember": True,
}

# after this many consecutive failed health checks, try a full re-initialize
_MAX_UNHEALTHY = 3


def detect_terminals():
    """Scan common install locations for terminal64.exe (Windows)."""
    found = []
    bases = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v:
            bases.append(v)
    bases.append(os.path.expanduser("~"))
    for base in bases:
        try:
            for p in glob.glob(os.path.join(base, "**", "terminal64.exe"), recursive=True):
                # keep it to plausible MT5 install dirs, skip deep junk
                if "MetaTrader" in p or "MT5" in p or "mql5" in p.lower():
                    found.append(p)
        except Exception:
            continue
    # also try the registry-less well-known default
    for p in (r"C:\Program Files\MetaTrader 5\terminal64.exe",
              r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"):
        if os.path.exists(p) and p not in found:
            found.append(p)
    return sorted(set(found))


class MT5Connection:
    def __init__(self, config_path):
        self.config_path = config_path
        self.cfg = dict(DEFAULTS)
        self._load()
        self._live = False
        self._last_error = ""
        self._busy = False          # a connect/repair is running
        self._unhealthy = 0
        self._lock = threading.Lock()
        self._stop = False
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog.start()

    # ------------------------------------------------------------ config
    def _load(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r") as f:
                    saved = json.load(f)
                for k in DEFAULTS:
                    if k in saved:
                        self.cfg[k] = saved[k]
        except Exception:
            pass

    def save(self, cfg):
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
        self.cfg = merged
        if merged.get("remember"):
            try:
                with open(self.config_path, "w") as f:
                    json.dump({k: v for k, v in merged.items() if k != "remember"},
                              f, indent=2)
            except Exception as e:
                return False, str(e)
        return True, ""

    def configured(self):
        return bool(self.cfg.get("login") or self.cfg.get("path") or self.cfg.get("server"))

    # ------------------------------------------------------------ health
    # _quick_health() is MT5 IPC - a sick terminal makes each call
    # hang for seconds, and status endpoints + the startup overlay poll it
    # constantly. Cache 10s; is_live_fresh() bypasses for the trade guard.
    _LIVE_TTL = 10

    def _quick_health(self):
        """Fast IPC check. Never raises."""
        if not HAS_MT5:
            return False
        try:
            ti = mt5.terminal_info()
            if ti is None or not getattr(ti, "connected", False):
                return False
            return mt5.account_info() is not None
        except Exception:
            return False

    def is_live(self):
        if not self._live:
            return False
        now = time.monotonic()
        if now - getattr(self, "_live_at", 0) < self._LIVE_TTL:
            return self._live_cache
        v = self._quick_health()
        self._live_cache = v
        self._live_at = now
        return v

    def is_live_fresh(self):
        """Uncached liveness - for the trade placement guard only."""
        if not self._live:
            return False
        v = self._quick_health()
        self._live_cache = v
        self._live_at = time.monotonic()
        return v

    def account_summary(self):
        try:
            acc = mt5.account_info()
            if acc is None:
                return None
            return {"login": acc.login, "server": acc.server, "name": acc.name,
                    "balance": acc.balance, "currency": acc.currency}
        except Exception:
            return None

    def status(self):
        acc = self.account_summary() if self._live else None
        try:
            ti = mt5.terminal_info() if (self._live and HAS_MT5) else None
            tpath = getattr(ti, "path", "") or ""
        except Exception:
            tpath = ""
        return {
            "has_mt5": HAS_MT5,
            "configured": self.configured(),
            "connected": self.is_live(),
            "busy": self._busy,
            "login": (acc or {}).get("login") or self.cfg.get("login") or "",
            "server": (acc or {}).get("server") or self.cfg.get("server") or "",
            "path": self.cfg.get("path") or "",
            "terminal_path": tpath,
            "error": self._last_error,
        }

    # ------------------------------------------------------------ connect
    def connect_async(self, cfg=None):
        """Start a (re)connect in the background; poll /api/mt5/status."""
        if cfg is not None:
            ok, msg = self.save(cfg)
            if not ok:
                return {"started": False, "error": "could not save settings: " + msg}
        with self._lock:
            if self._busy:
                return {"started": False, "error": "connect already in progress"}
            self._busy = True
        t = threading.Thread(target=self._connect_sync, daemon=True)
        t.start()
        return {"started": True}

    def _fail(self, msg):
        self._last_error = msg
        self._live = False
        self._live_at = 0  # drop the is_live() cache on transition
        return {"ok": False, "error": msg}

    def _connect_sync(self):
        cfg = self.cfg
        try:
            if not HAS_MT5:
                return self._fail("MetaTrader5 Python package not installed. "
                                  "Run: pip install MetaTrader5 (on Windows)")

            # Step 1: attach to a running terminal (fastest, most common case).
            # With no args it uses the last account the terminal had.
            init_args = {}
            if cfg.get("path"):
                init_args["path"] = cfg["path"]
            init_args["timeout"] = int(cfg.get("timeout") or 60000)
            if cfg.get("portable"):
                init_args["portable"] = True
            if not mt5.initialize(**init_args):
                err = mt5.last_error()
                return self._fail(f"Could not start/attach to MT5 terminal "
                                  f"(path='{cfg.get('path') or 'auto'}'): {err}. "
                                  f"Check the .exe path, or open MT5 manually first.")

            # Step 2: make sure we're on the wanted account (cheap switch,
            # no terminal restart). login() is the documented way to change
            # accounts on a live terminal connection.
            want_login = str(cfg.get("login") or "").strip()
            if want_login:
                acc = mt5.account_info()
                if acc is None or str(acc.login) != want_login:
                    if not mt5.login(login=int(want_login),
                                     password=str(cfg.get("password") or ""),
                                     server=str(cfg.get("server") or ""),
                                     timeout=int(cfg.get("timeout") or 60000)):
                        err = mt5.last_error()
                        return self._fail(
                            f"Login {want_login} failed: {err}. Check password "
                            f"and the exact server name (as shown in MT5's "
                            f"Navigator).")
            # Step 3: verify the broker session is really up.
            if not self._quick_health():
                return self._fail("Terminal started but not connected to the trade "
                                  "server (no internet? broker down?).")
            self._live = True
            self._live_at = 0  # drop the is_live() cache on transition
            self._unhealthy = 0
            self._last_error = ""
            return {"ok": True}
        except Exception as e:
            return self._fail(f"Unexpected error: {e}")
        finally:
            with self._lock:
                self._busy = False

    def disconnect(self):
        try:
            if HAS_MT5:
                mt5.shutdown()
        except Exception:
            pass
        self._live = False
        self._last_error = ""
        return {"ok": True}

    # ------------------------------------------------------------ watchdog
    def _watchdog_loop(self):
        """Every 30s: if we were live and health broke, repair quietly."""
        while not self._stop:
            time.sleep(30)
            try:
                if not self._live or self._busy or not HAS_MT5:
                    continue
                if self._quick_health():
                    self._unhealthy = 0
                    continue
                self._unhealthy += 1
                if self._unhealthy < _MAX_UNHEALTHY:
                    continue
                # Repair: fast path first (re-login on the same terminal),
                # full re-initialize only if that fails.
                with self._lock:
                    if self._busy:
                        continue
                    self._busy = True
                try:
                    want_login = str(self.cfg.get("login") or "").strip()
                    repaired = False
                    if want_login:
                        try:
                            repaired = bool(mt5.login(
                                login=int(want_login),
                                password=str(self.cfg.get("password") or ""),
                                server=str(self.cfg.get("server") or ""),
                                timeout=int(self.cfg.get("timeout") or 60000)))
                        except Exception:
                            repaired = False
                    if repaired and self._quick_health():
                        self._live = True
                        self._unhealthy = 0
                        self._last_error = ""
                    else:
                        # slow path: full re-initialize (terminal may have died)
                        res = self._connect_sync()
                        if not res.get("ok"):
                            self._live = False
                finally:
                    with self._lock:
                        self._busy = False
            except Exception:
                continue

    def stop(self):
        self._stop = True
