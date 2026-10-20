# Setup

## Requirements

### Operating system

- **Windows** is required for the MT5 data source (the MetaTrader 5
  terminal and the `MetaTrader5` Python package are Windows-only).
- Any system with Python 3 works for the OANDA data source (no MT5
  needed).

### Software

- **Python 3** — the app is a Python/Flask server. Install from
  [python.org](https://www.python.org/downloads/). During install on
  Windows, tick "Add Python to PATH" so `python` and `pip` work from a
  terminal.
- **A MetaTrader 5 terminal**, installed and logged in to your broker
  account — only needed for live MT5 data. The app auto-detects common
  install locations (`Program Files`, `Program Files (x86)`, your home
  folder) and the well-known default paths; if it cannot find the
  terminal, enter the path to `terminal64.exe` in the MT5 settings.
- **Internet access** — to install Python packages (`pip`) and to reach
  the data feed (OANDA API) and the Telegram API (if you enable Telegram
  alerts). The MT5 feed itself talks to the terminal on your own machine.

### Disk and memory

- A few hundred MB for the app, its Python packages, and the local
  cache. History caches grow with use: one-minute bars and the tick
  vault are stored in the data directory (see below). No exact size is
  guaranteed — it depends on how many symbols and how much history you
  pull.

## Install

```sh
git clone <repo-url>
cd stocksophist-charts
pip install -r requirements.txt
```

The packages are listed in `requirements.txt`:

| Package | Purpose |
|---|---|
| Flask >= 3.0 | Local web server |
| pandas >= 2.0 | History handling |
| pyarrow >= 14.0 | Parquet cache files |
| MetaTrader5 >= 5.0.45 (Windows only) | MT5 data source |
| pywebview >= 4.4 | Optional dedicated window on Windows |

### Virtualenv (optional)

To keep the packages isolated from your system Python:

```sh
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

`start_chart.bat` uses the system `python`; if you use a virtualenv,
activate it first and run `python app/server.py` yourself.

## Run

On Windows, double-click `start_chart.bat`. The script:

1. Stops any previous chart server (by PID file, window title, and
   port 5000) so an old version can never keep running underneath the
   new files. It never touches the MT5 terminal.
2. Waits up to 10 seconds for port 5000 to free up. If the port is still
   busy it stops with an error instead of starting a broken second
   server.
3. Runs `pip install -r requirements.txt`.
4. Starts `python app/server.py`. The server opens the chart in your
   browser automatically.

On other systems:

```sh
python app/server.py
```

The server listens on `http://127.0.0.1:5000`.

To stop the server on Windows, run `stop_chart.bat`. It stops only the
chart server (PID file, window title, command line, port 5000); it never
touches the MT5 terminal.

## Port and startup options (environment variables)

| Variable | Effect |
|---|---|
| `STOCKSOPHIST_CHART_PORT` | Server port (default `5000`). |
| `STOCKSOPHIST_NO_BROWSER=1` | Do not open the browser automatically. |
| `STOCKSOPHIST_NO_AUTO_MT5=1` | Do not auto-connect to MT5 at startup. |
| `STOCKSOPHIST_DATA_DIR` | Folder holding the external history database (see [docs/CONFIGURATION.md](CONFIGURATION.md)). This is **not** the runtime data directory. |
| `OANDA_TOKEN` / `OANDA_ACCOUNT_ID` | OANDA credentials (alternative to entering them in settings). |

Example (Windows Command Prompt):

```bat
set STOCKSOPHIST_CHART_PORT=5001
python app\server.py
```

## First run

On first run the server creates an empty data directory and connects to
the configured data feed. It syncs recent 1-minute history in the
background; the browser shows the sync progress, then the chart.

### What a healthy first run looks like

1. The server console prints that it is starting on
   `http://127.0.0.1:5000`.
2. The browser opens to the chart page with a loading overlay showing
   startup progress (MT5 connection, history warm-up).
3. The overlay clears and the chart paints candles for the detected
   symbol (usually XAUUSD in MT5 mode).
4. The footer badge shows the live feed label (LIVE-MT5 or LIVE-OANDA)
   and no STALE badge once ticks arrive.
5. `GET /api/health` returns `stream_up: true` and
   `quote_stale: false` once ticks are flowing (see
   [docs/API.md](API.md)).

If the chart stays on the loading overlay, check
[docs/FAQ.md](FAQ.md) and `GET /api/startup_status` for the current
startup stage and any error.

### Without MT5 (DEMO mode)

If no MT5 terminal is connected and no demo data files are present, the
chart shows:

> MT5 is not connected and no demo data is available. Open the MT5 button
> (top bar) and connect your terminal first.

DEMO mode reads local parquet demo files when they exist
(`<data-dir>/parquet/XAUUSD_1m_*.parquet`). Without them, connect MT5
(below) or switch the data source to OANDA (below). DEMO mode never
fabricates prices: with no demo files it shows the message above.

## Data source: MT5

1. Open the MT5 terminal and log in to your broker account.
2. In the chart app, use the MT5 button in the top bar to connect.
   Connection runs in the background; poll the MT5 status (the app shows
   it) for the result. The connection settings are: terminal path
   (blank = auto-detect or attach to a running terminal), login, password,
   server, timeout (default 60000 ms), portable, remember.
3. Once connected, the app pulls live quotes and history from the
   terminal and caches them locally.
4. In MT5 mode the watchlist follows the terminal's Market Watch. Use
   **Sync watchlist** to pull MT5's list into the app; symbols the app
   has never cached get a background history backfill.

If the terminal is not found, use **Detect paths** (the app scans the
standard install locations) or enter the full path to `terminal64.exe`
manually. After 3 consecutive failed health checks the app tries a full
re-initialize of the MT5 connection.

## Data source: OANDA

### Getting an OANDA practice account

The app only uses OANDA's **practice** (demo) environment — never a
live, funded account. A practice account is free, holds virtual funds,
and needs no deposit. If you do not have one yet:

1. Go to [oanda.com](https://www.oanda.com) and open a **practice /
   demo account** (email address and a password; pick the practice
   account, not a live trading account).
2. Sign in to your practice account hub and open **Manage API Access**.
3. Choose **Generate** to create a personal access token, and copy it.
   Keep the token secret — anyone with it can read your practice
   account through the API. You can regenerate it in the same place at
   any time (regenerating replaces the old token).
4. Note your **account ID** from the same hub. It looks like
   `101-001-XXXXXXX-001`. The account ID is not a secret.

### Connecting the app

1. In the app settings, open the data-feed settings and set the data
   source to OANDA.
2. Enter your OANDA account ID and API token. The token is stored on the
   server and is never returned by the settings API (only whether a token
   is saved). You can also provide them via the `OANDA_TOKEN` /
   `OANDA_ACCOUNT_ID` environment variables.
3. Restart the app. Changing the data source or the OANDA credentials
   takes effect on restart.
4. OANDA uses the **practice** environment only
   (`api-fxpractice.oanda.com` for REST,
   `stream-fxpractice.oanda.com` for the live pricing stream). History
   comes from `GET /v3/instruments/{XAU_USD}/candles`; live ticks come
   from the `POST /v3/accounts/{id}/pricing/stream` stream. If the
   stream goes quiet for 45 seconds the quote is marked stale and the app
   falls back to REST.

In OANDA mode, watchlist additions persist in `settings.json` under
`oanda_extra_symbols` so they survive restarts.

### If OANDA is not available in your country

OANDA only offers accounts in the countries it is licensed in, and
its signup form does not accept other countries. Two things are worth
knowing:

- **The app never needs a live OANDA account.** It reads prices and
  history from a practice account only, and it never places trades
  through OANDA.
- **Once a practice token exists, the app uses it directly.** The
  practice API hosts are the same worldwide, so day-to-day use needs
  no VPN and no special network setup.

If the signup itself is blocked in your country, the practical routes
are:

1. **Use the MT5 data source instead.** Any broker account with
   XAUUSD in MT5 gives the app the same chart and history, with no
   OANDA account at all.
2. **Open the practice account through a VPN.** Some users complete
   the OANDA practice signup while connected through a VPN endpoint
   in a country OANDA serves, then use the practice token with the
   app from home. Whether that is permitted is governed by OANDA's
   own terms of service, which can change — the account is yours, so
   check the current terms and decide for yourself. If OANDA closes
   such an account, the app simply falls back to MT5 or DEMO mode.

## Telegram alerts

Alerts are delivered to Telegram through a bot you own:

1. In Telegram, open BotFather and create a bot. Copy the bot token.
2. Find your own chat ID (for example, by messaging the bot and reading
   the chat ID from the bot API, or by using a channel/group ID you
   control).
3. In the chart app, open Settings, then Telegram, and enter:
   - Enable: on
   - Bot token: the token from BotFather
   - Target chat ID: your chat ID
   - Price alerts: on/off
   - Sweep / indicator alerts: on/off
4. Press **Send test** to verify delivery. If the test fails, check the
   token, the chat ID, and that the bot has permission to message the
   target chat.

The bot token is stored on the server and is never returned by the
settings API (only whether a token is saved). Leaving the token field
blank when saving keeps the stored token.

## Data directory

Runtime files (settings, alert definitions, alert log, caches) live
outside the app folder:

- Windows: `C:\stocksophist\cache`
- Other systems: `~/.stocksophist_chart_cache`

Because data lives outside the code folder, replacing the app folder with
a newer version keeps your settings, alerts, and cache. See
[docs/CONFIGURATION.md](CONFIGURATION.md) for the full file layout.

## Updating

1. Run `stop_chart.bat` (Windows) or stop the server process.
2. Replace the app folder with the new version (extract over it, or
   extract to a new folder and copy your `settings.json` next to the new
   `server.py` before first launch — migration moves it into the data
   directory permanently).
3. Start the app again. Your settings, alerts, and cache survive because
   they live in the data directory, not the code folder.

## Uninstall / cleanup

1. Stop the server (`stop_chart.bat` on Windows, or stop the process).
2. Delete the app folder (the cloned repository).
3. Delete the runtime data directory if you want a full cleanup:
   - Windows: `C:\stocksophist\cache`
   - Other systems: `~/.stocksophist_chart_cache`
   This removes settings, alert definitions, the alert log, caches, the
   tick vault, and custom indicator scripts stored in the data directory.
4. Delete the PID file if one remains: `%TEMP%\stocksophist_chart_server.pid`
   on Windows.
5. (Optional) Delete the external history database folder
   (`C:\stocksophist\data` by default, or wherever
   `STOCKSOPHIST_DATA_DIR` points). The app only reads this folder; it
   never deletes it.

Uninstalling does not touch your MT5 terminal, your broker account, or
your Telegram bot.
