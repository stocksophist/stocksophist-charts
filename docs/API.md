# API Reference

Base URL: `http://127.0.0.1:5000` (or the port set by
`STOCKSOPHIST_CHART_PORT`). All responses are JSON unless noted
(CSV export, tone audio). Errors are JSON `{"error": "..."}` with an
HTTP status code — never a bare Flask 500 page.

Conventions used below:

- `GET` routes take query parameters. `POST` routes take a JSON body
  unless noted (multipart for file uploads).
- `symbol` defaults to the feed's detected symbol (usually XAUUSD).
- `upto` (unix seconds) truncates a series for bar replay: the server
  only sees bars at or before the replay cursor.
- `fast=1` on `/api/candles` serves the disk cache only (zero MT5
  calls) for instant first paint.

## Page

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Chart UI (single page). |

## Status and health

| Method | Path | Purpose / key fields |
|---|---|---|
| GET | `/api/status` | Live/demo mode and feed summary. Fields: `version`, `mode` (`LIVE-MT5` / `LIVE-OANDA`), `feed_source` (`mt5` / `oanda`), `feed_label`, `symbol`, `tz`, `utc_offset_h`, `server_time`, `tick_vault`, `alert_engine` (engine stats + `running`), `feed_degraded`, `hist_circuit` (`closed` / `open` / `half-open`). |
| GET | `/api/health` | Feed health for the STALE badge. Fields: `feed` (`mt5` / `oanda`), `stream_up` (bool — stream connected, or terminal + feed live), `secs_since_tick` (float or null), `secs_since_heartbeat` (float or null — OANDA stream heartbeat age), `quote_stale` (bool — no tick in the last 45 s), `last_bar_time` (newest cached M1 bar, unix s, or null), `hist_sync` (`running` / `idle`). On error returns the same shape with safe defaults. |
| GET | `/api/version` | Server version. The frontend checks it on load; a mismatch means the Python process is older than the opened page (stale server). Response: `{"version": "3.4.43"}`. |
| GET | `/api/startup_status` | What the server is doing during startup (for the loading UI). Fields: `mt5_live`, `feed_label`, `warming`, `warmed`, `message`, `warmed_bars`, `ticks_today` (vault ticks today, −1 on error), `server_time`, `hist_circuit`, `startup_op`, `tickgap_op`. |
| GET | `/api/ops` | Live progress of every background operation. Response: `{"ops": [...], "server_time": ...}`. Each op has stage, optional 0–100 percent, a detail line, and an error on failure. |
| GET | `/api/ops/<op_id>` | Single-op snapshot for focused polling. Returns `{"missing": true, "id": ...}` when no such op exists. |
| POST | `/api/time_resync` | Force a fresh MT5-clock-offset measurement. Returns `{"utc_offset_h": ..., "ok": true}`. Use when the chart shows the wrong time (the offset may have been measured while MT5 was sick). |

## Candles, ticks, quotes, Renko

| Method | Path | Purpose / key parameters |
|---|---|---|
| GET | `/api/candles` | OHLC candles. Query: `symbol`, `tf` (1s/15s rejected — use `/api/tickbars`), `bars` (default 600, max 200000), `upto`, `fast=1`. Response: `{"symbol", "tf", "bars": [...], "earliest": ...}`. Errors: `400 unknown timeframe`, `502` with the real reason (e.g. OANDA returned no data). |
| GET | `/api/tickbars` | Sub-minute bars (1s/15s) from MT5 tick history. Query: `symbol`, `sec` (must be `1` or `15`), `bars` (default 400, max 20000). |
| GET | `/api/ticks` | Recent tick-by-tick bid/ask. Query: `symbol`, `count` (default 500, max 5000). Response: `{"symbol", "ticks", "resolved_from", "resolve_note"}`. |
| GET | `/api/quote` | Latest bid/ask. Query: `symbol`, `tf`, `mode` (`candles` / `renko`). Response: `{"symbol", "quote", "resolved_from", "resolve_note", ...}`. |
| GET | `/api/prev_close` | Previous daily close for a symbol. Query: `symbol`. Response: `{"symbol", "prev_close"}`. |
| GET | `/api/renko` | Renko bricks. Query: `symbol`, `box` (default 2.5, clamped 0.1–50), `bricks` (default 5000, max 20000), `tf` (base timeframe the bricks are built from, default `1m`), `upto` (replay: rebuilds from base bars ≤ cursor, drops the forming brick). |
| POST | `/api/renko/warm` | Warm the Renko brick base in the background. Body: JSON (symbol / box / timeframe). |
| GET | `/api/diag/gaps` | Gap diagnostic: which slots are missing from served bars, what MT5 itself returns, and what the tick vault covers. Query: `symbol`, `tf` (default `1m`), `bars` (default 600). |

## Indicators

| Method | Path | Purpose / key parameters |
|---|---|---|
| GET | `/api/indicators` | Indicator library catalogue. Response: `{"indicators": [...], "errors": [...]}`. Each entry: `id`, `name`, `author`, `description`, `attribution`, `overlay`, `inputs` (settings schema). Broken custom scripts land in `errors` instead of breaking the catalogue. |
| GET | `/api/indicator/<ind_id>` | Computed indicator overlays for a pane. Query: `symbol`, `tf`, `mode` (`candles` / `renko`), `box` (Renko box, default 2.5), `settings` (JSON object), `upto`. Response: the indicator's `compute()` result: `{"lines", "dashboard", "sweeps", "values"}`. Errors: `404 unknown indicator`, `501` when the feed is not live, `503` on the 120-second compute deadline, `{"loading": true}` while Renko bricks are still building. |
| POST | `/api/indicators/add` | Add a user indicator script. Body: `{"id", "name", "code"}`. The script's `META id` must match `id`. |
| POST | `/api/indicators/delete` | Delete a user indicator script. Body: `{"id"}`. |
| POST | `/api/indicators/favorite` | Star / unstar an indicator. Body: `{"id", "favorite"}`. |
| GET | `/api/indicator_settings` | Saved global indicator defaults. Response: `{"ok": true, "indicator_settings": {id: {key: value}}}`. |
| POST | `/api/indicator_settings` | Save or reset indicator defaults. Body: `{"indicator": id, "settings": {...}}` to save, or `{"indicator": id, "reset": true}` to restore factory defaults (deletes the saved override). Validation errors return `400` and change nothing. |

## Alerts

| Method | Path | Purpose / key fields |
|---|---|---|
| GET | `/api/alerts` | List alert definitions. Response: `{"alerts": [...]}`. |
| POST | `/api/alerts` | Create an alert. Body: the alert definition (symbol, condition, price / indicator, timeframe, mode `once` / `every`, cooldown minutes, expiry, delivery channels, message). Response: `{"ok": true, "alert": {...}}`. |
| PUT | `/api/alerts/<alert_id>` | Update an alert. Body: fields to change. `404` when not found. |
| DELETE | `/api/alerts/<alert_id>` | Delete an alert. `404` when not found. |
| POST | `/api/alerts/<alert_id>/stop` | Stop an alert manually → `stopped_manual`. |
| POST | `/api/alerts/<alert_id>/resume` | Resume a stopped alert → `active`. |
| GET | `/api/alerts/log` | Trigger log. Query: `limit` (default 200, max 1000), `offset`, `since` (unix seconds — only events after this). |
| DELETE | `/api/alerts/log` | Clear the trigger log. |
| GET | `/api/alerts/log.csv` | Trigger log as CSV download (`stocksophist_alerts_log.csv`). |
| POST | `/api/alerts/fire` | Single fire path for price alerts and indicator signals. Price body: `{"alert_id", "price"}`. Indicator body: `{"kind": "indicator", "indicator_id", "indicator_name", "symbol", "tf", "text", "price", "sweep_key", "event", "settings", "chart_mode": "candles"\|"renko", "box"}`. |
| POST | `/api/alerts/tone` | Upload a custom alert tune. Multipart field `file`; accepted types: mp3, wav, ogg, m4a, flac. Stored as `tones/custom.<ext>` in the data directory, replacing any previous custom tune. |
| GET / HEAD | `/api/alerts/tone` | Serve the stored **custom** tune audio (404 JSON when none uploaded). HEAD returns headers only. |

## Telegram, webhook, feed settings

| Method | Path | Purpose / key fields |
|---|---|---|
| GET | `/api/telegram/config` | Saved Telegram settings. The token is **never** returned — only `has_token`. Response: `{"enabled", "has_token", "chat_id", "on_price", "on_sweep"}`. |
| POST | `/api/telegram/config` | Save Telegram settings. Body: `{"enabled", "chat_id", "on_price", "on_sweep", "token"}`. Blank token keeps the stored one. Response: `{"ok": true, "has_token"}`. |
| POST | `/api/telegram/send` | Alert dispatch: Telegram + the global outbound webhook. Body: `{"text", "kind": "price"\|"sweep"\|"" , "skip_webhook", "symbol", "price"}`. `text` is required. |
| GET | `/api/webhook/config` | Global **outbound** webhook settings. Response: `{"enabled", "url", "has_url"}`. |
| POST | `/api/webhook/config` | Save the global outbound webhook. Body: `{"enabled", "url"}`. Blank URL keeps the stored one. |
| POST | `/api/webhook/fire` | Outbound webhook for per-indicator alerts. Body: `{"url"` (optional — falls back to the saved global URL), `"text", "symbol", "price", "indicator", "event"}`. The POST is done server-side so browser CORS is never an issue and the URL never has to be exposed to the page. |
| GET | `/api/feed/config` | Data-feed settings. The OANDA token is **never** returned — only `has_token`. Response: `{"data_source": "mt5"\|"oanda", "active_source", "oanda": {"has_token", "account_id"}}`. Changing data source or credentials requires a restart. |
| POST | `/api/feed/config` | Save data-feed settings. Body: `{"data_source", "account_id", "token"}`. Blank token keeps the stored one. Response: `{"ok": true, "has_token", "restart_required": true, "message": "Saved. Restart the chart app to switch the data feed."}`. |

## MT5 connection

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/mt5/status` | Connection status. Fields: `has_mt5`, `configured`, `connected`, `busy`, `login`, `server`, `path`, `terminal_path`, `error`. |
| GET | `/api/mt5/detect_paths` | Scan standard install locations for `terminal64.exe`. Response: `{"paths": [...]}`. |
| POST | `/api/mt5/connect` | Save settings (if `remember`) and (re)connect in the background. Body: connection config (`path`, `login`, `password`, `server`, `timeout`, `portable`, `remember`). Poll `/api/mt5/status` for the result — never blocks. |
| POST | `/api/mt5/disconnect` | Disconnect from the terminal. |
| POST | `/api/repair_gaps` | Manually trigger the MT5 gap-fill, bypassing the hourly throttle. Body/query: `symbol`. Returns how many bars were filled. |

## History, backfill, backup, ticks

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/backfill/start` | Start a manual deep backfill (M1 + ticks) in the background. Body: `{"symbol", "tick_days"}` (default `tick_days` 15). Returns a job id. |
| GET | `/api/backfill/status` | Backfill job status. Query: `job_id`. |
| GET | `/api/history_status` | Status of the external standalone history database. Query: `refresh=1` forgets the cached handle so a newly dropped-in `.db` file is picked up without restarting. |
| GET | `/api/db_path` | Where the app looks for the history database, including a debug listing of every searched folder. |
| POST | `/api/db_path` | Save a manual history-database override. Body: `{"path": "..."}` (persisted in `settings.json` as `db_path_override`); `{"path": ""}` clears it. The file must exist. |
| GET | `/api/browse` | Server-side folder browser for picking the `.db` file (browsers cannot reveal real paths via `<input type=file>`). Query: `path`. Returns `{"path", "dirs", "files", "parent"}`. |
| POST | `/api/backup/create` | Export all cached history to a single timestamped `.db` file. Body/query: `symbol`. |
| GET | `/api/backup/list` | List backup files. |
| POST | `/api/backup/restore` | Restore a backup file, then gap-fill missing ranges from MT5. Body: `{"symbol", "file"}`. |
| POST | `/api/import/history` | Upload a CSV/TXT 1-minute history file and merge it into the database. Multipart field `file`. |
| GET | `/api/incr/status` | List pending incremental files for a symbol. Query: `symbol`. |
| POST | `/api/incr/merge` | Manually merge incrementals into the main database. Body: `{"symbol"}`. |
| GET | `/api/ticks/gap` | Tick-gap report for the standalone history database. |
| POST | `/api/ticks/backfill` | Start a tick-gap backfill. Body: `{"symbol"}`. Returns `{"job_id"}`. |
| POST | `/api/ticks/backup_incr` | Fetch a date range into a separate incremental `.db` file. Body: `{"symbol", "start_date", "end_date"}`. |
| GET | `/api/ticks/incr_list` | List incremental backup files. |
| POST | `/api/ticks/merge_incr` | Merge incremental files into the main DB. Body: `{"files": [...]}` (defaults to all listed incrementals). Returns `{"job_id"}`. |
| GET | `/api/ticks/job/<jid>` | Status of a tick-manager job. |
| POST | `/api/ticks/import_parquet` | Upload a tick parquet file into the main DB. Multipart field `file`. |

## Data healing, history warm-up, watchlist, symbols, news

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/data/heal` | Manual "Fix data": starts a background heal job and returns immediately. Body (optional): `{"symbol", "tf"}`. With no body, heals every recently-used series. Poll `GET /api/data/heal/status?job=<id>` for progress. |
| GET | `/api/data/heal/status` | Poll a heal job. Query: `job`. `404` for an unknown job id. |
| POST | `/api/history/deep` | Deep-history pull for a symbol. Body: JSON (symbol / options). |
| GET | `/api/history/deep/status` | Deep-history status. Query: `symbol`. |
| POST | `/api/history/warm` | Warm history for a symbol. Body: JSON (symbol / options). |
| GET | `/api/watchlist` | Watchlist. Response: `{"live", "watchlist", "feed_label"}`. |
| POST | `/api/watchlist/add` | Add a symbol. Body: `{"symbol"}`. |
| POST | `/api/watchlist/remove` | Remove a symbol. Body: `{"symbol"}`. |
| POST | `/api/watchlist/sync` | Sync the watchlist with MT5's Market Watch (MT5 is the source of truth). Optional body: `{"keep": ["XAUUSD", ...]}` prunes everything else. Runs as a background op; symbols never cached get a background history backfill. |
| GET | `/api/symbols/search` | Search symbols the feed offers. Query: `q`. Response: `{"symbols": [...]}`. |
| GET | `/api/news` | News events for one IST calendar day (default: today). Times are IST `HH:MM` plus epoch `ts`. Range mode: with `from`/`to` (unix seconds) returns USD high-impact events (plus Medium when `medium=1`) inside the range as `{"ok", "from", "to", "events", "count", "fetched_at"}`. Display only. |
| POST | `/api/news/refresh` | Force an immediate calendar re-fetch. |

## Notes

- There is **no inbound webhook / relay**: the app does not accept
  alerts from other systems. The only webhook is the app's own
  **outbound** webhook (alert delivery).
- There is **no chart-AI endpoint** and no signal-generation endpoint.
  Indicators are computed on request; alerts are evaluated by the
  background engine.
- All state-changing routes that accept JSON use `request.get_json(
  force=True, silent=True)` and treat a missing/invalid body as `{}`.
