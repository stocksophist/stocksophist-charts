# Usage

## Chart basics

- **Timeframes:** 1s, 15s, 1m, 3m, 5m, 15m, 30m, 1H, 4H, D, W, M.
  Switch the timeframe from the timeframe control. The default favorite
  set is 1m, 3m, 5m, 15m, 1H, D.
- **Panes:** indicators marked "On chart" draw over the price candles.
  The rest draw in a separate pane below the price chart.
- **Candles / Renko:** the chart shows candlesticks by default. The Renko
  view shows Renko bricks built from the chart's own bars. The default
  box size is 2.5 price units (adjustable, range 0.1–50). Renko bricks
  come from the chart's timeframe bars: a 15-second pane builds bricks
  from 15-second bars.
- **Layouts:** chart layouts and named layouts are saved in the browser
  (local storage). They survive app restarts on the same browser and
  profile.

## Watchlist

The watchlist lists the symbols the app tracks. Click a symbol to show
its chart. Add a symbol from the Add dialog, which searches the symbols
the configured data feed offers. Remove a symbol from the watchlist when
you no longer need it.

## Indicators

Add an indicator from the indicator menu. Every indicator row in the
legend has a gear icon that opens its settings (see
[docs/INDICATORS.md](INDICATORS.md)). Indicator settings save globally
and persist across restarts.

## Alerts

The app has its own alert system with price alerts and indicator-condition
alerts:

- Create an alert from the alert dialog. An indicator alert stores the
  timeframe its indicator is evaluated on, and it is evaluated on that
  timeframe no matter which timeframe the chart pane currently shows.
- Alert states: active, stopped manually, stopped after triggering, or
  stopped after expiring.
- Alert evaluation has a cooldown, a per-alert frequency setting, and an
  expiry. An anti-spam guard auto-stops an alert that triggers more than
  15 times within 3 minutes.
- When an alert triggers, it is written to the alert log and delivered
  to Telegram if Telegram delivery is enabled (see
  [docs/SETUP.md](SETUP.md)). An optional global outbound webhook can
  also receive alert events; its URL is configured in settings and is
  never returned by the settings API.
- The alert log lives in the data directory (`alert_log.json`) and can be
  exported as CSV (`stocksophist_alerts_log.csv`) from the app.

Alerts are chart alerts: they notify you. They do not place trades or
send orders.
