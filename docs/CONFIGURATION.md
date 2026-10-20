# Configuration

All settings live in `settings.json` inside the data directory. The
server reads it on startup and on every settings request; writes are
atomic (tmp file + `os.replace`). On first run the file does not exist
and defaults apply; it is created when settings are first saved. If a
`settings.json` sits next to `server.py` (from an older install), it is
migrated into the data directory once, then the in-tree copy is ignored.

## Data directory

| System | Path |
|---|---|
| Windows | `C:\stocksophist\cache` |
| Other systems | `~/.stocksophist_chart_cache` |

The full file layout is in [docs/ARCHITECTURE.md](ARCHITECTURE.md)
("Where state lives").

## `settings.json` keys

| Key | Type | Default | What it controls |
|---|---|---|---|
| `telegram` | object | `{}` | Telegram delivery. See "Telegram" below. |
| `oanda` | object | `{}` | OANDA credentials. See "OANDA" below. |
| `data_source` | string | `"mt5"` | Active data feed: `"mt5"` or `"oanda"`. Changing it requires a restart (the feed object is created at startup). |
| `indicator_settings` | object | `{}` | Saved global indicator defaults: `{indicator_id: {key: value}}`. See [docs/INDICATORS.md](INDICATORS.md). |
| `webhook` | object | `{}` | Global outbound webhook. See "Outbound webhook" below. |
| `oanda_extra_symbols` | list | `[]` | Extra watchlist symbols in OANDA mode (persisted automatically when you add a symbol). |
| `db_path_override` | string | (absent) | Manual history-database path override, set via `POST /api/db_path`. Empty / absent = auto-discovery. |

`mt5_connection.json` (same directory, separate file) holds the MT5
connection settings: `path`, `login`, `password`, `server`, `timeout`
(default 60000 ms), `portable`, `remember`. It is **not** part of
`settings.json`.

## Telegram

`settings.json` → `telegram`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | false | Master switch for Telegram delivery. |
| `token` | string | (absent) | Bot token from BotFather. **Never returned by the API** — `GET /api/telegram/config` returns only `has_token`. Blank on save keeps the stored token. |
| `chat_id` | string | `""` | Target chat / group / channel ID the bot messages. |
| `on_price` | bool | true | Deliver price alerts. |
| `on_sweep` | bool | true | Deliver sweep / indicator alerts. |

Delivery is per-alert too: each alert has its own Telegram checkbox.
An alert is delivered only when both the global settings and the
alert's own channel are on.

## OANDA

`settings.json` → `oanda`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `token` | string | (absent) | OANDA API token. **Never returned by the API** — `GET /api/feed/config` returns only `has_token`. Blank on save keeps the stored token. |
| `account_id` | string | `""` | OANDA account ID. Returned by the API (it is not a secret). |

Environment variables `OANDA_TOKEN` / `OANDA_ACCOUNT_ID` are used as a
fallback when the settings are empty. Changing the OANDA credentials or
`data_source` requires a restart.

OANDA uses the **practice** environment only:
`https://api-fxpractice.oanda.com` (REST) and
`https://stream-fxpractice.oanda.com` (pricing stream). There is no
live-environment support.

## Outbound webhook

`settings.json` → `webhook`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | false | Master switch for the global outbound webhook. |
| `url` | string | `""` | Destination URL. Blank on save keeps the stored URL. |

When an alert fires with webhook delivery on, the server POSTs JSON to
the URL: `{"event", "indicator", "symbol", "price", "text", "time"}`.
The POST is done server-side, so browser CORS is never an issue. The
URL is never returned by the settings API (`GET /api/webhook/config`
returns only `has_url`). Per-alert webhook URLs (entered in the alert
dialog) override the global URL for that alert.

This is the app's **own outbound** webhook (alert delivery). The app
does not accept inbound webhooks from other systems.

## Environment variables

Only these are honored. Anything else is ignored.

| Variable | Honored by | Effect |
|---|---|---|
| `STOCKSOPHIST_CHART_PORT` | `server.py`, `chart_window.py` | Server port (default `5000`). |
| `STOCKSOPHIST_NO_BROWSER` | `server.py` | `=1`: do not open the browser automatically at startup. |
| `STOCKSOPHIST_NO_AUTO_MT5` | `server.py` | `=1`: do not auto-connect to MT5 at startup. |
| `STOCKSOPHIST_DATA_DIR` | `history_db.py`, `tick_manager.py` | Folder holding the **external history database** (default `C:\stocksophist\data` on Windows). This is **not** the runtime data directory — it points at the user-managed `.db` file location. Search order: this variable, then `C:\stocksophist\data`, then a sibling `../data` folder, then `./data` in the app tree, then `~/stocksophist_data`. |
| `OANDA_TOKEN` | `oandafeed.py` | OANDA API token (fallback when settings are empty). |
| `OANDA_ACCOUNT_ID` | `oandafeed.py` | OANDA account ID (fallback when settings are empty). |
| `TEMP` / `TMP` | `server.py` | Folder for the server PID file on Windows (`stocksophist_chart_server.pid`). Falls back to `C:\Windows\Temp`. |
| `ProgramFiles` / `ProgramFiles(x86)` | `mt5conn.py` | Search roots for `terminal64.exe` detection. |

No other environment variables are read. In particular, there is no
environment override for the runtime data directory (`DATA_DIR`) or
for the Telegram token.

## History database location

The external standalone history database (`.db` file) is found by:

1. `POST /api/db_path` override (`db_path_override` in `settings.json`),
   if set.
2. `STOCKSOPHIST_DATA_DIR`, if set.
3. `C:\stocksophist\data` (Windows default).
4. A sibling `../data` folder next to the app folder.
5. `./data` inside the app tree (legacy / dev).
6. `~/stocksophist_data`.

`GET /api/history_status` reports the resolved database and its status;
`GET /api/history_status?refresh=1` forgets the cached handle so a
newly dropped-in `.db` file is picked up without restarting.
`GET /api/db_path` lists every searched folder for debugging.

## Secrets handling

- The Telegram bot token and the OANDA API token are stored in
  `settings.json` in the data directory. They are **never** returned by
  any API endpoint — only `has_token` booleans.
- The MT5 account password is stored in `mt5_connection.json` in the
  data directory. It is never returned by the API (`/api/mt5/status`
  returns login and server, never the password).
- Tokens are never written to logs, never baked into releases, and never
  sent to the browser.
- `settings.json` and `mt5_connection.json` are excluded from release
  ZIPs and from the repository (see `.gitignore`).
