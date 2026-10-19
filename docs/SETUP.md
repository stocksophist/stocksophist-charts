# Setup

## Requirements

- Windows (for the MT5 data source), or any system with Python 3 for the
  OANDA data source
- Python 3
- A MetaTrader 5 terminal, installed and logged in, for live MT5 data
- Internet access (to install Python packages and to reach the data feed)

## Install

```sh
git clone <repo-url>
cd stocksophist-charts
pip install -r requirements.txt
```

The pinned packages are listed in `requirements.txt` (Flask, pandas,
pyarrow, and the MetaTrader 5 package on Windows).

## Run

On Windows, double-click `start_chart.bat`. The script installs the
requirements, stops any previous chart server, starts the new server, and
the chart opens in your browser automatically.

On other systems:

```sh
python app/server.py
```

The server listens on `http://127.0.0.1:5000`. To use a different port,
set the `STOCKSOPHIST_CHART_PORT` environment variable before starting.
To skip the automatic browser open, set `STOCKSOPHIST_NO_BROWSER=1`.

To stop the server on Windows, run `stop_chart.bat`. It stops only the
chart server; it never touches the MT5 terminal.

## First run

On first run the server creates a fresh settings file and an empty data
directory. It connects to the configured data feed and syncs recent
1-minute history in the background; the browser shows the sync progress,
then the chart.

### Without MT5 (DEMO mode)

If no MT5 terminal is connected and no demo data files are present, the
chart shows:

> MT5 is not connected and no demo data is available. Open the MT5 button
> (top bar) and connect your terminal first.

DEMO mode reads local parquet demo files when they exist. Without them,
connect MT5 (below) or switch the data source to OANDA (below).

## Data source: MT5

1. Open the MT5 terminal and log in to your broker account.
2. In the chart app, use the MT5 button in the top bar to connect.
   Connection runs in the background; the MT5 status endpoint reports the
   result.
3. Once connected, the app pulls live quotes and history from the
   terminal and caches them locally.

## Data source: OANDA

1. In the app settings, open the data-feed settings and set the data
   source to OANDA.
2. Enter your OANDA account ID and API token. The token is stored on the
   server and is never returned by the settings API (only whether a token
   is saved).
3. Restart the app. Changing the data source or the OANDA credentials
   takes effect on restart.

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
a newer version keeps your settings, alerts, and cache.
