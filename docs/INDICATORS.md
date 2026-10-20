# Indicators

StockSophist Charts ships with 17 standard indicators. Each indicator is
a reference display: it draws lines, bands, or bars on the chart. An
indicator by itself does not generate signals.

Overlay indicators draw on the price chart. The rest draw in a separate
pane below the price chart.

Indicators compute on closed bars only. On a Renko pane they compute on
bricks. Indicator values are never used to place trades.

## Changing settings

Every indicator row in the chart legend has a gear icon. Click it to open
the indicator's settings dialog, which has two groups:

- **Inputs** — lengths, sources, levels, multipliers.
- **Style** — colors, line widths, line styles, and per-line show/hide
  toggles.

**Save** stores the new values as the global default for that indicator
(they persist across restarts). **Reset** restores the factory defaults
shown in the table below (it deletes the saved override for that
indicator).

## Settings persistence

- Indicator settings are **global**, not per-symbol and not per-chart:
  one saved default per indicator id, stored in `settings.json` under
  `indicator_settings` as `{indicator_id: {key: value}}`.
- When an indicator is computed, the server starts from the factory
  defaults and applies the saved global override on top. Unknown keys in
  a saved override are ignored, so a stale override from an older
  version can never inject junk.
- Settings are validated against the indicator's schema (types, min/max
  ranges). An invalid value is rejected with an error; the stored
  settings are not changed.
- Favorites (starred indicators) are stored separately in
  `indicators/favorites.json` inside the data directory.
- Computed indicator results are cached on the server per
  indicator/symbol/timeframe/settings in the data directory's
  `indicator_cache/` folder, and the cache is invalidated when the
  underlying bars change. Renko-mode and bar-replay computations skip
  the cache.

## Indicator table

| Indicator | What it computes | What it draws | Default inputs | Placement |
|---|---|---|---|---|
| EMA x4 | Four exponential moving averages (each: today's price weighted by `2 / (length + 1)` plus yesterday's EMA) | Four EMA lines | Lengths 9 / 21 / 50 / 200, source Close; colors blue / orange / green / red; width 1; style Solid; all four lines shown | On chart |
| EMA Line | A single exponential moving average | One EMA line | Length 50, color blue, width 2 | On chart |
| SMA | Simple moving average: the mean of the last N source prices | One SMA line | Length 20, source Close, color amber, width 1 | On chart |
| RSI | Wilder RSI: smoothed average gains vs smoothed average losses, mapped to 0–100; flat market = 50 | RSI line + 70 / 30 levels + latest-value dashboard | Length 14, source Close, overbought 70, oversold 30, line color purple, width 2, levels shown | Separate pane |
| MACD | MACD line = fast EMA − slow EMA; signal = EMA of the MACD line; histogram = MACD − signal | MACD line, signal line, histogram | Fast 12, slow 26, signal 9; MACD blue, signal orange, histogram up green / down red; width 2; all parts shown | Separate pane |
| Bollinger Bands | Middle = SMA; upper/lower = SMA ± multiplier × sample standard deviation | Middle line, two bands, fill between the bands | Length 20, multiplier 2.0; mid orange, bands blue; width 1; middle, bands, and fill shown; fill blue at 0.08 opacity | On chart |
| VWAP | Volume-weighted average price: cumulative (typical price × volume) / cumulative volume, anchored to the day (or week); bands at ±1 / 2 / 3 volume-weighted standard deviations | VWAP line + three sigma bands | Source Typical (HLC/3), anchor Day; line orange, width 2, style Solid; bands shown (1 / 2 / 3 sigma), band color gray, width 1, style Dashed. Uses MT5 tick volume; bars without volume are weighted equally. | On chart |
| ATR | Wilder ATR: smoothed average of the true range (max of high−low, |high−prev close|, |low−prev close|) | ATR line | Length 14, color teal, width 2 | Separate pane |
| Stochastic | %K = (close − lowest low) / (highest high − lowest low) × 100 over N bars; %D and slow %D are 3-bar smoothings of %K | %K, %D, slow %D lines + 80 / 20 levels | %K length 14, %D smoothing 3, slow %D smoothing 3, overbought 80, oversold 20; %K blue, slow %D orange; width 2; levels shown | Separate pane |
| ADX | Wilder ADX: smoothed directional movement (+DM / −DM) normalized into +DI / −DI, then the smoothed absolute difference as trend strength | ADX, +DI, −DI lines + trend-strength level | Length 14, level 25; ADX orange, +DI teal, −DI red, level gray; width 2; all lines and level shown | Separate pane |
| Volume | Per-bar volume (MT5 tick volume: number of price updates per bar) | Bar-volume histogram colored by bar direction | Up-bar green, down-bar red; width 1 | Separate pane |
| OBV | On-Balance Volume: running total of volume, added on up bars and subtracted on down bars | OBV line | Line blue, width 2, style Solid | Separate pane |
| CCI | Commodity Channel Index: (typical price − its N-bar mean) / (0.015 × mean deviation) | CCI line + +100 / −100 levels | Length 20; line purple, width 2; +100 / −100 levels shown | Separate pane |
| Williams %R | (Highest high − close) / (highest high − lowest low) × −100 over N bars | %R line + −20 / −80 levels | Length 14; line orange, width 2; −20 / −80 levels shown | Separate pane |
| Parabolic SAR | Parabolic Stop and Reverse: a trailing stop that accelerates toward price and flips side when price crosses it | SAR dots trailing price | Acceleration 0.02, max acceleration 0.2; up-trend dots green, down-trend dots red | On chart |
| Ichimoku Cloud | Tenkan = (9-bar high + 9-bar low) / 2; Kijun = (26-bar high + 26-bar low) / 2; Senkou A = (Tenkan + Kijun) / 2 plotted 26 bars ahead; Senkou B = (52-bar high + 52-bar low) / 2 plotted 26 bars ahead; Chikou = close plotted 26 bars behind | Tenkan, Kijun, Senkou A, Senkou B, Chikou lines + cloud fill between Senkou A and B | Tenkan 9, Kijun 26, Senkou B 52, displacement 26; Tenkan blue, Kijun orange, Senkou A teal, Senkou B red, Chikou gray; width 1; all lines and cloud fill shown; cloud opacity 0.08 | On chart |
| Supertrend | ATR bands (basic upper/lower = HL/2 ± multiplier × ATR) that flip trend when price closes through the band | One trend line that follows price and flips color on trend change | ATR length 10, multiplier 3.0; up-trend green, down-trend red; width 2 | On chart |

Colors in the table are the factory defaults. Any color, width, style,
length, or level can be changed from the indicator's settings dialog.

## Custom indicator scripts

The indicator library also loads user-authored Python scripts from the
`custom/` folder inside the data directory's `indicators/` folder. A
script must define `META` (id, name, author, description, overlay),
`INPUTS` (the settings schema), and `compute(ctx)` (returning lines,
dashboard, sweeps, values) — the same contract as the built-in files.
Scripts are validated on load (id must be 2–40 chars: lowercase letters,
digits, underscore) and a broken script is reported in the catalogue's
`errors` list instead of breaking the app. See
[docs/DEVELOPMENT.md](DEVELOPMENT.md) for the full pattern.
