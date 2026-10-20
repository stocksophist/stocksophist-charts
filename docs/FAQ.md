# FAQ / Troubleshooting

## The chart shows a STALE badge

STALE means no tick (price update) has arrived for the last 45 seconds.
This is expected when the market is closed — for example on weekends —
because no new ticks are produced. The badge clears on the first tick
after the market reopens. If the badge stays on during market hours,
check the data-feed connection (MT5 terminal logged in, or OANDA
credentials saved) and your internet connection.

`GET /api/health` shows the same fact as data: `quote_stale: true` plus
`secs_since_tick` (how old the last tick is). A STALE badge with
`stream_up: true` and a fresh heartbeat usually means the market is
closed, not that the connection is broken.

## Port 5000 is already in use

`start_chart.bat` waits up to 10 seconds for port 5000 to free up. If it
is still busy, it stops with an error. Close any other copy of the chart
server (or run `stop_chart.bat`), and make sure no zombie Python process
is holding the port. Then run `start_chart.bat` again.

To run on a different port instead, set `STOCKSOPHIST_CHART_PORT` before
starting (for example `set STOCKSOPHIST_CHART_PORT=5001`), and open
`http://127.0.0.1:5001` yourself.

## MT5 is not connected

If the chart reports that MT5 is not connected and no demo data is
available, the MT5 terminal is not linked to the app. Open the MT5
terminal, log in to your broker account, then use the MT5 button in the
app's top bar to connect. Connection runs in the background; the MT5
status indicator reports the result.

### The MT5 terminal is not running

The app can attach to an already-running terminal, or launch one at the
configured path. If it cannot find the terminal, use **Detect paths**
(the app scans `Program Files`, `Program Files (x86)`, your home folder,
and the well-known default install locations for `terminal64.exe`) or
enter the full path to `terminal64.exe` in the MT5 settings. Check
`/api/mt5/status`: `has_mt5: false` means the Python `MetaTrader5`
package is not installed (Windows only); `configured: false` means no
login/server is saved; `error` carries the last connection error text.

### MT5 connects but history is missing

Use **Fix missing candles** (top bar) to rescan for gaps and backfill
from MT5, or **Deep history** for the maximum history the broker serves.
`GET /api/diag/gaps?tf=1m&bars=600` reports which slots are missing from
the served bars, what MT5 itself returns, and what the tick vault
covers. History healing runs automatically too: a background loop heals
gaps (first run 60s after startup, then every 10 minutes), and the tick
vault (the app's own 1-second recording of every quote) fills holes that
MT5's rate history skips.

## OANDA errors

- **OANDA is not available in my country — can I still use the OANDA
  feed?** The app needs only a free practice (demo) account; it never
  trades through OANDA. If OANDA's signup does not accept your
  country, see "If OANDA is not available in your country" in
  [SETUP](SETUP.md) — including the VPN route for the signup and the
  MT5 alternative.
- **"OANDA returned no M1 data" / empty chart in OANDA mode:** the
  account may have no access to the symbol, or the instrument name may
  differ. The app resolves symbols through the feed; check
  `/api/status` for the detected symbol and the feed label.
- **401 / unauthorized:** the token is wrong, expired, or belongs to a
  different OANDA environment. The app uses the practice environment
  only. Re-enter the token in the data-feed settings and restart.
- **Stream quiet, REST fallback:** if the pricing stream pushes nothing
  for 45 seconds, the quote is marked stale and the app falls back to
  REST polling. Check your internet connection and the OANDA status.
- **Changing OANDA credentials or the data source does not take
  effect:** restart the app. The feed object is created at startup, so
  feed changes require a restart.

## The chart is empty / shows no candles

1. Check the feed: MT5 connected, or OANDA credentials saved and the
   app restarted after changing them.
2. Check `/api/health`: `stream_up` and `quote_stale` tell you whether
   ticks are flowing.
3. Check the symbol: the chart may be showing a symbol the feed has no
   data for. Switch to the detected symbol (usually XAUUSD).
4. In DEMO mode with no demo files, the chart shows the "MT5 is not
   connected and no demo data is available" message instead of candles.
   Connect MT5 or switch to OANDA.
5. The app retries a failed candle load up to 3 times with backoff, so a
   transient failure recovers on its own. If the chart stays blank,
   check the data-feed connection and reload the page.

## An alert is not firing

Check, in order:

1. The alert is `active` (not stopped manually, triggered, or expired).
   The alert panel lists each alert's state.
2. The alert's timeframe: an indicator alert is evaluated on the
   timeframe stored when it was created, not the timeframe the chart
   currently shows. A 1H alert will not fire on a 1m chart's bars.
3. The alert's condition and price/level: a price alert fires on a
   cross of the price, evaluated on closed bars.
4. Cooldown: an `every` alert waits out its cooldown between firings.
5. Expiry: an alert past its expiry moves to `stopped_expired`.
6. Anti-spam: an alert that fired more than 15 times within 3 minutes
   is auto-stopped.
7. Delivery: if the alert fires (it appears in the alert log) but no
   Telegram message arrives, see "Telegram alerts are not arriving"
   below. The alert log is the ground truth for whether an alert fired.

## Telegram alerts are not arriving

Check, in order:

1. Telegram delivery is enabled in Settings → Telegram.
2. A bot token is saved (the settings show only whether a token is saved,
   never the token itself). If no token is saved, enter one from
   BotFather.
3. The target chat ID is correct and the bot has permission to message
   that chat.
4. The matching toggle is on: Price alerts for price alerts, Sweep /
   indicator alerts for indicator alerts.
5. Press **Send test** in the Telegram settings. If the test fails, the
   token or chat ID is wrong, or the bot cannot message the target.
6. Check the alert log: if the alert never fired, the problem is the
   alert (see above), not Telegram.

## The outbound webhook is not receiving events

1. The webhook URL is saved in settings (Settings → webhook). A blank
   URL keeps the stored one.
2. The alert's webhook delivery checkbox is on (per-alert), or the
   global webhook is enabled.
3. The receiving server must accept a JSON POST with fields `event`,
   `indicator`, `symbol`, `price`, `text`, `time`. The server-side POST
   avoids browser CORS issues; a failure returns an error to the alert
   log, not a silent drop.
4. The webhook URL is never returned by the settings API; if you need to
   verify it, re-enter it.

## An indicator shows an error / is missing

- `GET /api/indicators` returns the catalogue plus an `errors` list.
  A broken custom script appears in `errors` and does not break the
  built-in indicators.
- `GET /api/indicator/<id>` returns `404 unknown indicator` for a bad
  id, `501` when the feed is not live (for example a request that needs
  live MT5), `503` when the 120-second compute deadline is exceeded, and
  `{"loading": true}` while Renko bricks are still building.
- If an indicator's settings dialog shows an error on save, one of the
  values failed validation (type or min/max range). The stored settings
  are not changed.

## Settings are not saving

Settings are stored in `settings.json` inside the data directory
(`C:\stocksophist\cache` on Windows, `~/.stocksophist_chart_cache`
elsewhere). Make sure the data directory is writable and that the app
process has permission to write there. Note that changing the data
source (MT5 / OANDA) or the OANDA credentials takes effect only after
restarting the app.

## The layout reset / settings look different

- **Layout reset:** chart layouts and named layouts are stored in the
  browser's local storage, not in the app's data directory. Opening the
  app in a different browser, a different browser profile, or after
  clearing the site's stored data resets the layout to the defaults.
- **Indicator settings reset:** indicator defaults live on the server
  in `settings.json` (`indicator_settings`). If they reset, the data
  directory may have been deleted or the app may be running from a
  different data directory (for example a different user account).
- **Browser cache issues:** if the page shows a version mismatch (the
  frontend checks `/api/version` on load and warns when the running
  Python process is older than the opened page), hard-reload the page
  (Ctrl+Shift+R) and make sure only one chart server is running
  (`stop_chart.bat`, then start again). Clearing the site's stored data
  also clears layouts, themes, and custom tunes stored in the browser.

## The server will not start

- **pip install fails:** check your internet connection. `pip install`
  needs to reach PyPI. You can start the server without reinstalling by
  running `python app/server.py` directly (it skips the pip step).
- **Port already in use:** see "Port 5000 is already in use" above.
- **Python not found:** install Python 3 and tick "Add Python to PATH"
  during install, or run with the full path to `python.exe`.
- **The server starts but the browser does not open:** open
  `http://127.0.0.1:5000` manually, or start with
  `STOCKSOPHIST_NO_BROWSER=1` and open the URL yourself.

## The Trade panel refuses / an order fails

- The Trade panel refuses when MT5 is not live. Connect MT5 first;
  DEMO mode can never send an order.
- An order that fails returns the MT5 error text. Common causes: the
  symbol is not in Market Watch, the volume is outside the broker's
  min/max/step, or the trade server rejected the price (requote).
- Alerts and indicators never place orders. Only an explicit action in
  the Trade panel sends an order.

## News is missing

The calendar fetches once every 4 hours and caches to
`ff_calendar_thisweek.json` in the data directory. Use the refresh
button to force an immediate re-fetch. If events still do not appear,
check your internet connection. News is display only; missing news
never blocks charting, indicators, or alerts.

## Backup / restore problems

- **Restore then gaps:** restore intentionally gap-fills missing ranges
  from MT5 after restoring, so a short delay after restore is normal.
- **A backup file is missing:** backups are listed by
  `GET /api/backup/list`. Backup files live in the data directory area
  managed by the backfill module; deleting the data directory deletes
  them.
- **Import fails:** `POST /api/import/history` accepts CSV/TXT 1-minute
  history files. A file in another format is rejected with an error.
