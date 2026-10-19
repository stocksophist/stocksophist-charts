# FAQ / Troubleshooting

## The chart shows a STALE badge

STALE means no tick (price update) has arrived for the last 45 seconds.
This is expected when the market is closed — for example on weekends —
because no new ticks are produced. The badge clears on the first tick
after the market reopens. If the badge stays on during market hours,
check the data-feed connection (MT5 terminal logged in, or OANDA
credentials saved) and your internet connection.

## Port 5000 is already in use

`start_chart.bat` waits up to 10 seconds for port 5000 to free up. If it
is still busy, it stops with an error. Close any other copy of the chart
server (or run `stop_chart.bat`), and make sure no zombie Python process
is holding the port. Then run `start_chart.bat` again.

## MT5 is not connected

If the chart reports that MT5 is not connected and no demo data is
available, the MT5 terminal is not linked to the app. Open the MT5
terminal, log in to your broker account, then use the MT5 button in the
app's top bar to connect. Connection runs in the background; the MT5
status indicator reports the result.

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

## Settings are not saving

Settings are stored in `settings.json` inside the data directory
(`C:\stocksophist\cache` on Windows, `~/.stocksophist_chart_cache`
elsewhere). Make sure the data directory is writable and that the app
process has permission to write there. Note that changing the data
source (MT5 / OANDA) or the OANDA credentials takes effect only after
restarting the app.

## The layout reset

Chart layouts and named layouts are stored in the browser's local
storage, not in the app's data directory. Opening the app in a different
browser, a different browser profile, or after clearing the site's
stored data resets the layout to the defaults.

## The chart is blank after a refresh

The app retries a failed candle load up to 3 times with backoff, so a
transient failure recovers on its own. If the chart stays blank, check
the data-feed connection and reload the page.
