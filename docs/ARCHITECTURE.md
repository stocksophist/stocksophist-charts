# Architecture

StockSophist Charts is a single-process Python server (`app/server.py`,
Flask) that serves a browser UI (`app/static/`). The browser renders
charts with the vendored Lightweight Charts library; all data,
indicator math, alert evaluation, and trading actions happen on the
server. The browser never talks to MT5, OANDA, or Telegram directly —
the server does all of that, so tokens and credentials never leave the
server.

## Code layout

```
stocksophist-charts/
├── app/
│   ├── server.py            # Flask server: HTTP API, page serving, background jobs
│   ├── mt5feed.py           # MT5 data feed (+ DEMO fallback)
│   ├── mt5conn.py           # MT5 connection manager (attach / launch / health)
│   ├── mt5trade.py          # Chart trading on MT5 (orders, SL/TP, close)
│   ├── oandafeed.py         # OANDA v20 data feed (same interface as MT5 feed)
│   ├── tickstore.py         # Tick vault: 1-second OHLC recording of every quote
│   ├── tick_manager.py      # Tick lifecycle: gap detection, backfill, backups, merge
│   ├── history_db.py        # Reader for the external standalone history database
│   ├── backfill.py          # Manual backfill, backup, restore, import jobs
│   ├── chart_window.py      # Dedicated window (pywebview) for the chart server
│   ├── ops.py               # Background-operation progress bus
│   ├── alert_defs.py        # Alert definitions + trigger log + fire rules
│   ├── renko.py             # Renko brick builder
│   ├── indicators/          # Indicator library (built-ins + user scripts)
│   │   ├── __init__.py      # Loader, validation, settings helpers, favorites
│   │   ├── adx.py ...       # One file per built-in indicator
│   │   └── custom/          # User-authored indicator scripts (empty by default)
│   ├── static/
│   │   ├── index.html       # Chart page
│   │   ├── app.js           # All frontend logic
│   │   ├── lightweight-charts.standalone.production.js  # Vendored charting lib
│   │   └── sound/alert.wav  # Bundled "Classic" alert tune
│   └── test_indicators.py   # Indicator test suite (unittest, no network)
├── requirements.txt
├── start_chart.bat          # Windows: stop old server, install deps, start
└── stop_chart.bat           # Windows: stop the chart server only
```

### What each module does

| Module | Responsibility |
|---|---|
| `server.py` | Flask app. Serves `/` (the chart UI) and ~65 `/api/*` routes. Owns the feed object, the data directory, settings load/save, the alert engine loop, the prefetch loop, the heal loop, the news loop, and all background threads. |
| `mt5feed.py` | MT5 data feed with graceful DEMO fallback. M1 history via `copy_rates_from_pos`, tick-by-tick via `copy_ticks_from` (every price change). Caches history as parquet in the data directory; resamples higher timeframes from M1; heals gaps from the tick vault. When no MT5 terminal is connected, falls back to DEMO mode (parquet demo files) or raises a clean "MT5 is not connected" error. |
| `mt5conn.py` | MT5 connection manager. Detects terminals by scanning for `terminal64.exe`, attaches to a running terminal or launches one at the configured path, tracks health, and re-initializes after 3 consecutive failed health checks. Connection settings live in `mt5_connection.json` in the data directory: path, login, password, server, timeout (default 60000 ms), portable, remember. |
| `mt5trade.py` | Direct chart trading on MT5: market/pending orders, SL/TP modify, close. Safety: every route refuses when the MT5 connection is not live (DEMO mode can never send an order); all `order_send` calls hold a single trade lock. |
| `oandafeed.py` | OANDA v20 data feed. Implements the same public interface as `mt5feed.Feed` so `server.py` can swap the feed object based on `settings["data_source"]` (`"mt5"` / `"oanda"`) with no other changes. Practice environment only: REST `api-fxpractice.oanda.com`, stream `stream-fxpractice.oanda.com`. History via `/v3/instruments/{XAU_USD}/candles`; live via the `/v3/accounts/{id}/pricing/stream` stream; 45-second stale threshold with REST fallback. |
| `tickstore.py` | Tick vault. Every quote the server sees is persisted as 1-second OHLC bars in SQLite (`ticks.db` in the data directory). Purpose: MT5's rate history can freeze or skip chunks while its tick stream keeps flowing; the vault is the app's own recording of the same ticks, so when history has a hole the chart heals the gap from the vault. |
| `tick_manager.py` | Tick-by-tick data lifecycle for the standalone history database: gap detection (compare the DB's newest tick vs now), gap backfill (fetch missing ticks day-by-day from MT5), incremental backup (fetch a date range into a separate `.db` file), merge incrementals into the main DB, import parquet tick files. |
| `history_db.py` | Reader for the external standalone history database. Reads only; the user manages the file. Searches, in order: the `STOCKSOPHIST_DATA_DIR` environment variable, `C:\stocksophist\data` (Windows default), a sibling `../data` folder, `./data` inside the app tree, and `~/stocksophist_data`. |
| `backfill.py` | Manual backfill, backup, and restore. Backfill: M1 bars as deep as the broker serves, tick-by-tick for the last N days, higher timeframes resampled from M1. Backup: export all cached history to a timestamped `.db` file. Restore: restore a backup, then gap-fill missing ranges from MT5. Import: merge an uploaded CSV/TXT 1-minute history file. |
| `chart_window.py` | Dedicated lightweight window. Runs the chart server as a separate process and shows it in a small dedicated browser window (pywebview, backed by Windows WebView2). Falls back to the system browser if the embedded window cannot start. |
| `ops.py` | Background-operation progress bus. Every long-running background job (startup history sync, Renko tick pull, tick-gap backfill, watchlist backfill) reports here: stage, optional 0–100 percent, a detail line, and — on failure — the real error. The UI polls `/api/ops` and narrates progress instead of showing static "Loading..." messages. |
| `alert_defs.py` | Alert definitions + trigger log. Definitions (price alerts + per-indicator alerts) in `alerts.json` with lifecycle states `active | stopped_manual | stopped_triggered | stopped_expired`. Trigger log in `alert_log.json` (capped at 5000, newest first, CSV export). Fire rules: cooldown / frequency / expiry / anti-spam (>15 triggers in 3 minutes → auto-stop). Persistent sweep dedup keys so a restart can never refire a sweep. All writes are atomic (tmp file + `os.replace`). |
| `renko.py` | Renko brick builder: turns a bar series into bricks of a fixed box size. |
| `indicators/` | Indicator library. See [docs/INDICATORS.md](INDICATORS.md) and [docs/DEVELOPMENT.md](DEVELOPMENT.md) for the module contract (`META` / `INPUTS` / `compute`). Built-ins live in this directory; user scripts live in `custom/` (inside the data directory's `indicators/` folder at runtime); favorites live in `favorites.json`. |

## Data flow

```
MT5 terminal ──(MetaTrader5 API)──┐
                                  ├─► Feed (mt5feed / oandafeed)
OANDA API ──(REST + stream)───────┘        │
                                            ▼
                              server.py: history cache (parquet, DATA_DIR)
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
              /api/candles            /api/indicator/<id>       /api/quote
              (OHLC bars)             (indicator math)           (latest tick)
                    │                       │                       │
                    └───────────────────────┼───────────────────────┘
                                            ▼
                              Browser: Lightweight Charts rendering
                                            │
                              Alert engine (3s loop) ─► Telegram / webhook
                              Tick recorder (1s) ─► tick vault (ticks.db)
```

1. **Feed** — the server holds one feed object. In MT5 mode it reads
   history and ticks from the terminal; in OANDA mode it reads history
   from REST and ticks from the pricing stream. Both feeds expose the
   same interface (`detect_symbol`, `get_quote`, `get_ticks`,
   history, `live`, `degraded`, `hist_circuit`).
2. **Cache** — history is cached as parquet files in the data directory
   (`<symbol>_<tf>.parquet` working set, `<symbol>_1m.archive.parquet`
   deep archive). Higher timeframes resample from M1. A `fast=1`
   request serves the disk cache only (zero MT5 calls) for instant
   first paint; the full deep load then runs in the background.
3. **Indicators** — computed on the server from the cached bars (or from
   Renko bricks in Renko mode), on closed bars only. Results are cached
   per indicator/symbol/timeframe/settings in `indicator_cache/`.
4. **Alerts** — evaluated by the background alert engine every 3
   seconds, independent of the browser. Firing goes through one fire
   path: log + popup + tune + Telegram / webhook.
5. **Ticks** — every quote is recorded once a second into the tick vault
   (`ticks.db`), even with no browser tab open, so future history gaps
   can be healed from the app's own recording.

## Where state lives

All runtime state lives in the **data directory**, outside the code
folder — so replacing the app folder with a newer version keeps
settings, alerts, and caches:

| Path (relative to data directory) | What it holds |
|---|---|
| `settings.json` | All settings: `telegram`, `oanda`, `data_source`, `indicator_settings`, `webhook`, `oanda_extra_symbols`, `db_path_override` |
| `mt5_connection.json` | MT5 connection settings (path, login, password, server, timeout, portable, remember) |
| `alerts.json` | Alert definitions + fired sweep-dedup keys |
| `alert_log.json` | Alert trigger log (capped at 5000) |
| `ticks.db` | Tick vault: 1-second OHLC bars of every recorded quote |
| `indicator_cache/` | Cached computed indicator results |
| `indicators/` | `favorites.json` (starred indicators) + `custom/` (user indicator scripts) |
| `renko/` | Renko brick-chain state |
| `tones/` | `custom.<ext>` — uploaded custom alert tune |
| `parquet/` | DEMO-mode parquet files (`XAUUSD_1m_*.parquet`) |
| `<symbol>_<tf>.parquet` | Working-set history cache per symbol/timeframe |
| `<symbol>_1m.archive.parquet` | Deep-history archive (1m) |
| `ff_calendar_thisweek.json` | Cached economic-calendar events |

The browser also holds state in local storage (same browser + profile
only): layouts, named layouts, themes, chart profiles, timeframe
favorites, custom tunes, and per-pane state. Clearing the site's stored
data resets those; server-side state in the data directory is
unaffected.

The external history database (standalone `.db` file) is **not** in the
data directory. It lives in `C:\stocksophist\data` by default, or
wherever `STOCKSOPHIST_DATA_DIR` points. The app only reads it.

## Background jobs

All jobs are daemon threads started by `server.py`. They keep running
with no browser tab open.

| Job | What it does | Cadence |
|---|---|---|
| Tick recorder | Records every quote into the tick vault once a second. | Every 1 s |
| Alert engine | Evaluates every active alert on its own timeframe. | Every 3 s |
| Data-integrity / heal loop | Rescans served series for gaps and backfills them from MT5. | First run 60 s after startup, then every 10 min |
| News loop | Refreshes the economic calendar. | Every 4 h (TTL) |
| Prefetch loop | Warms history for watched symbols in the background. | 25 s after start, then every 120 s |
| Tick-gap check | At startup, compares the tick vault against now and reports missing ranges. | At startup |
| Deep-sync worker | Pulls deep history for symbols in the background queue. | Continuous while queued |
| Startup warm history | Warms the default symbol's history so first paint is fast. | At startup |
| Boot deep queue | Queues deep-history pulls for cached symbols. | At startup |
| Renko base pull | Builds the Renko brick base for 1s/15s panes in the background. | On demand |
| Tick-vault prune | Prunes old vault rows. | At startup (background) |
| Backfill / heal / merge jobs | On-demand jobs from the API (backfill, backup, restore, heal, tick backfill/merge). | On demand; progress via the ops bus (`/api/ops`) |

The ops bus (`ops.py`) tracks every long-running job with stage,
percent, detail, and error. The UI polls `/api/ops` and narrates
progress. Each indicator compute also runs under a 120-second overall
deadline; a deadline breach returns an honest 503 instead of hanging
the request.
