# Indicators

StockSophist Charts ships with 17 standard indicators. Each indicator is
a reference display: it draws lines, bands, or bars on the chart. An
indicator by itself does not generate signals.

Overlay indicators draw on the price chart. The rest draw in a separate
pane below the price chart.

## Changing settings

Every indicator row in the chart legend has a gear icon. Click it to open
the indicator's settings dialog, which has two groups:

- **Inputs** — lengths, sources, levels, multipliers.
- **Style** — colors, line widths, line styles, and per-line show/hide
  toggles.

**Save** stores the new values as the global default for that indicator
(they persist across restarts). **Reset** restores the factory defaults
shown in the table below.

## Indicator table

| Indicator | What it draws | Default inputs | Placement |
|---|---|---|---|
| EMA x4 | Four exponential moving averages in one indicator | Lengths 9 / 21 / 50 / 200, source Close; colors blue / orange / green / red; width 1; style Solid; all four lines shown | On chart |
| EMA Line | A single exponential moving average | Length 50, color blue, width 2 | On chart |
| SMA | Simple moving average | Length 20, source Close, color amber, width 1 | On chart |
| RSI | Relative Strength Index (Wilder) | Length 14, source Close, overbought 70, oversold 30, line color purple, width 2, levels shown | Separate pane |
| MACD | MACD line, signal line, histogram | Fast 12, slow 26, signal 9; MACD blue, signal orange, histogram up green / down red; width 2; all parts shown | Separate pane |
| Bollinger Bands | SMA with upper/lower bands at N standard deviations, with fill between the bands | Length 20, multiplier 2.0; mid orange, bands blue; width 1; middle, bands, and fill shown; fill blue at 0.08 opacity | On chart |
| VWAP | Volume-weighted average price with standard-deviation bands | Source Typical (HLC/3), anchor Day; line orange, width 2, style Solid; bands shown (1 / 2 / 3 sigma), band color gray, width 1, style Dashed. Uses MT5 tick volume; bars without volume are weighted equally. | On chart |
| ATR | Average True Range (Wilder) | Length 14, color teal, width 2 | Separate pane |
| Stochastic | Stochastic oscillator: %K, %D, slow %D | %K length 14, %D smoothing 3, slow %D smoothing 3, overbought 80, oversold 20; %K blue, slow %D orange; width 2; levels shown | Separate pane |
| ADX | Average Directional Index (Wilder) with +DI / -DI and a trend-strength level | Length 14, level 25; ADX orange, +DI teal, -DI red, level gray; width 2; all lines and level shown | Separate pane |
| Volume | Bar-volume histogram | Up-bar green, down-bar red; width 1 | Separate pane |
| OBV | On-Balance Volume (cumulative volume signed by price direction) | Line blue, width 2, style Solid | Separate pane |
| CCI | Commodity Channel Index | Length 20; line purple, width 2; +100 / -100 levels shown | Separate pane |
| Williams %R | Williams %R oscillator | Length 14; line orange, width 2; -20 / -80 levels shown | Separate pane |
| Parabolic SAR | Parabolic Stop and Reverse dots trailing price | Acceleration 0.02, max acceleration 0.2; up-trend dots green, down-trend dots red | On chart |
| Ichimoku Cloud | Tenkan, Kijun, Senkou A, Senkou B, Chikou, and the cloud fill | Tenkan 9, Kijun 26, Senkou B 52, displacement 26; Tenkan blue, Kijun orange, Senkou A teal, Senkou B red, Chikou gray; width 1; all lines and cloud fill shown; cloud opacity 0.08 | On chart |
| Supertrend | ATR bands that flip when price closes through the band | ATR length 10, multiplier 3.0; up-trend green, down-trend red; width 2 | On chart |

Colors in the table are the factory defaults. Any color, width, style,
length, or level can be changed from the indicator's settings dialog.
