# Usage

## Top bar

The top bar holds the app's main controls (hover a button for its exact
label):

| Button | What it does |
|---|---|
| Watchlist | Show / hide the symbol watchlist. |
| Chart / canvas settings | Chart and canvas settings (colors, volume display, timezone, and related display options). |
| MT5 connection settings | MT5 connection: terminal path, login, password, server, connect / disconnect. |
| Telegram alert settings | Telegram bot token, target chat, price / indicator toggles, Send test. |
| Chart trading (MT5) | Trade panel: market/pending orders, SL/TP modify, close. Requires live MT5. |
| Chart layout | Chart layout: 1, 2, 3 or 4 charts side by side. |
| Save layout now | Save the layout immediately. An orange dot means unsaved changes (layouts also auto-save every 60s). |
| Chart profile | Switch the chart profile. |
| Backfill history / backup / restore | Manual history backfill, backup, restore, and history import. |
| Fix missing candles | Rescan for gaps and backfill them from MT5. |
| Deep history | Maximum MT5 history with progress. |
| Economic calendar | Today's news events (IST). Display only. |
| USD news markers | USD news markers on all charts: red = High impact, gold = Medium. Display only. |
| Bar replay | Step through history bar by bar. |
| Switch theme | Cycle the UI theme: Warm dark, OLED black, Light. |

The footer badge shows the feed label (LIVE-MT5 / LIVE-OANDA) and the
STALE badge when no tick has arrived for 45 seconds.

## Chart basics

- **Timeframes:** 1s, 15s, 1m, 3m, 5m, 15m, 30m, 1H, 4H, D, W, M.
  Switch the timeframe from the timeframe control. The default favorite
  set is 1m, 3m, 5m, 15m, 1H, D (stored in the browser; change it from
  the timeframe menu).
- **Panes:** each chart pane has a symbol, a timeframe, and a chart mode
  (Candles or Renko). Indicators marked "On chart" draw over the price
  candles; the rest draw in a separate pane below the price chart. Each
  pane has its own settings (symbol, timeframe, mode, box size) and its
  own indicator legend with a gear icon per indicator.
- **Chart interactions:** the chart area supports the chart library's
  standard interactions — drag to pan, scroll to zoom, crosshair on
  hover, and a price line for the last price. The pane header holds the
  pane's symbol, timeframe, and mode controls.
- **Drawings:** drawings are saved per layout in the browser. Right-click
  a chart to open the context menu: **Add horizontal line here** drops a
  horizontal line at that price, and **Remove all drawings** clears
  unlocked drawings. Each drawing remembers its own style (color, width,
  line style). Drag a drawing to move it.
- **Keyboard:** `Escape` closes the alert panel (when no dialog is
  open) and cancels bar-replay picking. There are no other global
  keyboard shortcuts; everything else is mouse-driven.
- **Themes:** the UI theme (Warm dark / OLED black / Light) is stored in
  the browser under `stocksophist-theme` (default: Warm dark). Chart
  canvas colors follow the UI theme unless you set explicit chart colors
  in Chart / canvas settings, which keep winning over the theme.
- **Bar replay:** the Replay control steps through history bar by bar.
  While replaying, the chart, indicators, and Renko bricks only see bars
  at or before the replay cursor — replay can never peek at future bars.
  `Escape` cancels replay picking. Replay is **synced across panes**:
  pick the start bar on any chart and every pane replays together, each
  in its own timeframe.
- **Zoom-sync:** press the **Zoom** button in the top bar, then drag a
  box on any chart. When you release, **every chart zooms to that time
  range** — each pane keeps its own timeframe, only the visible time
  window changes. It works once per press; press Zoom again for another
  selection, or `Escape` to cancel while it is armed.

## Watchlist

The watchlist lists the symbols the app tracks. Click a symbol to show
its chart. Add a symbol from the Add dialog, which searches the symbols
the configured data feed offers. Remove a symbol from the watchlist when
you no longer need it.

- In **MT5 mode** the terminal's Market Watch owns the list. Use **Sync
  watchlist** to pull MT5's list into the app; symbols the app has never
  cached get a background history backfill. You can pass a `keep` list
  to prune everything else.
- In **OANDA mode** additions persist in `settings.json` under
  `oanda_extra_symbols` so they survive restarts.

## Candles / Renko

The chart shows candlesticks by default. Switch a pane to **Renko** from
the pane's mode control; a box-size input appears next to it.

- **Box size:** default 2.5 price units, adjustable, range 0.1–50. The
  server clamps out-of-range values to that range.
- **How bricks build:** bricks are built from the pane's timeframe bars
  — a 15-second pane builds bricks from real 15-second bars (live MT5
  ticks); 1m and above build from that timeframe's bars. Bricks come
  from a stable incremental chain shared with Renko-pane indicators, so
  the visible boxes do not jump when the feed revises its tail.
- **Live updates:** new bricks appear as price moves by one box; the
  forming (incomplete) brick is shown until it closes.
- **Replay:** in bar replay, bricks rebuild from base bars at or before
  the replay cursor and the forming brick is dropped, so a replayed
  Renko chart shows exactly the closed bricks known at that moment.
- **Indicators on Renko:** an indicator on a Renko pane computes on the
  bricks, not on candles. An indicator alert on a Renko pane is
  evaluated on bricks built from its stored timeframe.
- **First load:** the first 1s/15s Renko load pulls tick history in the
  background; the pane shows a loading overlay until the brick base is
  ready, then paints.

## Layouts

Chart layouts and named layouts are saved in the browser (local
storage). They survive app restarts on the same browser and profile.

- **Save layout now** saves immediately; an orange dot on the button
  means there are unsaved changes. Layouts also auto-save every 60s.
- **Named layouts** let you keep several saved layouts and switch
  between them.
- **Chart profiles** switch the whole chart profile (panes, symbols,
  indicators) at once.
- Layouts store panes, symbols, timeframes, modes, indicators and their
  settings, and drawings. Indicator *defaults* (the gear-icon settings)
  are stored separately on the server in `settings.json` — see
  [docs/INDICATORS.md](INDICATORS.md).

## Indicators

Add an indicator from the indicator menu. Every indicator row in the
legend has a gear icon that opens its settings (see
[docs/INDICATORS.md](INDICATORS.md)). Indicator settings save globally
and persist across restarts.

Indicators compute on closed bars only. On a Renko pane they compute on
bricks. Results are cached on the server per indicator/symbol/timeframe/
settings so reopening a chart is fast; the cache is invalidated when the
underlying bars change.

## Alerts

The app has its own alert system with price alerts and indicator-condition
alerts.

### Creating an alert

- **Price alert:** symbol, condition (cross above / cross below a
  price), price, name, note, mode (once / every), cooldown between
  alerts (minutes, only for "every"), expiry, and delivery channels
  (popup, sound, Telegram, webhook, custom webhook URL, custom message
  text).
- **Indicator alert:** indicator, condition, timeframe (the alert is
  evaluated on this timeframe only, no matter which timeframe the chart
  pane shows), mode, cooldown, expiry, and the same delivery channels.
  A Renko indicator alert also stores the chart mode and box size.

### Alert lifecycle

1. **Create** — the alert is stored in `alerts.json` (data directory)
   with state `active`.
2. **Evaluate** — the background alert engine runs every 3 seconds and
   evaluates each active alert on its own timeframe, on closed bars.
   The browser does not need to be open.
3. **Fire** — when the condition is met, the alert fires through the
   single fire path: the trigger is written to the alert log, a popup
   appears (if enabled), the alert tune plays (if enabled), and the
   Telegram message / outbound webhook are sent (if enabled).
4. **After firing** — a `once` alert moves to `stopped_triggered`. An
   `every` alert stays active but waits out its cooldown before it can
   fire again.
5. **Stop / resume / expire** — stop an alert manually at any time
   (`stopped_manual`); resume it to make it active again. An alert past
   its expiry moves to `stopped_expired`.
6. **Anti-spam** — an alert that triggers more than 15 times within
   3 minutes is auto-stopped. A popup has a one-click stop button for a
   runaway alert.

Alert states: `active`, `stopped_manual`, `stopped_triggered`,
`stopped_expired`.

### Alert log and delivery

- The alert log lives in the data directory (`alert_log.json`, capped
  at 5000 entries, newest first) and can be exported as CSV
  (`stocksophist_alerts_log.csv`) from the app.
- Delivery channels per alert: popup, sound, Telegram, outbound webhook.
  Telegram delivery is gated by the Telegram settings (enabled + Price
  alerts toggle for price alerts, Sweep / indicator alerts toggle for
  indicator alerts).
- **Alert tunes:** built-in tunes are synthesized in the browser (no
  files). The "Classic" tune plays the bundled `alert.wav`. You can
  upload a custom tune (mp3, wav, ogg, m4a, flac); it is stored as
  `tones/custom.<ext>` in the data directory and replaces any previous
  custom tune. A custom tune can also be stored in the browser's local
  storage.
- **Message text:** the alert dialog's message field supports a
  `{{text}}` placeholder for the generated alert text.

Alerts are chart alerts: they notify you. They do not place trades or
send orders.

## Chart trading (MT5)

The Trade panel (top bar) is the only place that can send orders, and
only as an explicit action by you:

- Place market / pending orders, modify SL/TP, close positions.
- Every order action refuses when the MT5 connection is not live — DEMO
  mode can never send an order, and the panel also blocks it. This is a
  backstop, not just a UI check.
- All order sends hold a single lock, so only one order goes to the
  terminal at a time.

Alerts and indicators never trigger trading. See
[DISCLAIMER.md](../DISCLAIMER.md).

## News

The Economic calendar button shows today's news events (IST). The USD
news markers toggle draws High-impact (red) and Medium-impact (gold)
USD events on all charts. News is display only: markers never gate
signals or alerts. The calendar refreshes every 4 hours, or immediately
via the refresh button.
