# StockSophist Charts

StockSophist Charts is a local charting application that runs on your own
computer. It draws candlestick charts and Renko bricks, computes standard
technical indicators, keeps a symbol watchlist, and can send chart alerts
to Telegram. Chart data comes from a MetaTrader 5 terminal or from OANDA.

Everything runs locally: the app is a Python/Flask server that serves the
chart UI to your browser. No account with StockSophist is needed and no
chart data leaves your machine except the requests your configured data
feed makes to its own provider.

## Features

- Candlestick charts with 12 timeframes: 1s, 15s, 1m, 3m, 5m, 15m, 30m,
  1H, 4H, D, W, M
- Renko bricks with an adjustable box size, built from the chart's own bars
- 17 standard indicators (see [docs/INDICATORS.md](docs/INDICATORS.md)),
  each with full Inputs + Style settings that persist across restarts
- Watchlist: add and remove symbols, switch the chart symbol from the list
- Chart panes: overlay indicators draw on the price chart; oscillator
  indicators get their own pane below it
- Alerts: price alerts and indicator-condition alerts, evaluated on the
  timeframe the alert was created for, delivered to Telegram and logged
  with a CSV export
- Data: live quotes and history from MT5 (Windows) or OANDA; local cache
  so reopening the app is fast
- Saved layouts stored in the browser, including named layouts

## Tech stack

- Python 3, Flask (local web server)
- pandas / pyarrow (history handling)
- MetaTrader 5 API (Windows only, for the MT5 data source)
- Lightweight Charts (vendored charting library, runs in the browser)

## Quick start

```sh
git clone <repo-url>
cd stocksophist-charts
pip install -r requirements.txt
```

On Windows, double-click `start_chart.bat`. On other systems:

```sh
python app/server.py
```

The server starts on `http://127.0.0.1:5000` and opens the chart in your
browser automatically. See [docs/SETUP.md](docs/SETUP.md) for full setup
instructions, including the MT5 connection and Telegram configuration.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — requirements, install, first run,
  MT5 and Telegram setup
- [docs/INDICATORS.md](docs/INDICATORS.md) — the 17 indicators and their
  default settings
- [docs/USAGE.md](docs/USAGE.md) — using the chart, watchlist, Renko,
  layouts, and alerts
- [docs/FAQ.md](docs/FAQ.md) — troubleshooting
- [DISCLAIMER.md](DISCLAIMER.md) — disclaimer

## Screenshots

Screenshots will be added here.

## License

License: see LICENSE

## Disclaimer

StockSophist Charts is provided for education only. It is not trading
advice, and it makes no profit promises. See
[DISCLAIMER.md](DISCLAIMER.md) for the full disclaimer.
