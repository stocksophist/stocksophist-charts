# Backtest (StockSophist Charts)

Simulation only. A backtest replays an indicator's BUY/SELL signals over
closed historical bars and reports what would have happened. **It never
places a live order** — the engine (`app/backtest.py`) imports no broker,
no MT5 trade path, and no order-sending code of any kind. Nothing in the
backtest path can touch a live account.

## How to run a backtest

1. Click **Backtest** in the topbar (next to Sync drawings).
2. Fill in the inputs:
   - Symbol, timeframe, optional date range (empty = all loaded history).
   - Indicator + its settings. v1 ships one reference provider:
     **Supertrend flip** — BUY when the Supertrend direction flips to
     up, SELL when it flips to down (ATR length + multiplier settings).
   - Capital, sizing (risk-per-trade in $ or a fixed lot), stop-loss
     (fixed price distance or ATR multiple), TP legs, the playbook
     preset, trailing, breakeven-after-TP1, exit timing (C2C),
     opposite-signal behaviour, max-bars / flat-at-close exits, and
     commission + slippage.
   - Robustness checks: walk-forward split, parameter sweep, Monte
     Carlo reshuffle, cost stress (all on by default).
3. Click **Run backtest**. Results show: gate badge, metrics, equity
   curve (strategy vs buy-and-hold), All/Long/Short splits, exit-reason
   breakdown, warnings, the trade table, robustness sections, the
   assumptions line, and the full input snapshot. **Download full trade
   ledger (CSV)** exports every column, not just the visible ones.

API (for scripts, same behaviour as the panel):

- `GET /api/backtest/providers` — provider catalogue + settings schema.
- `POST /api/backtest/run` — JSON body `{symbol, tf, start, end,
  provider, provider_settings, config, walk_forward, sweep, monte_carlo,
  cost_stress}`. Returns metrics, splits, trades, legs, equity curves,
  exit breakdown, gates, history quality, robustness, snapshot,
  assumptions, warnings, and a `run_id`.
- `GET /api/backtest/trades?run=<id>&format=csv` — full ledger CSV.
  Run results are kept server-side for the last 5 runs only.

## The fill contract (plain words)

These rules are locked in code (`app/backtest.py`) and printed on every
report's assumptions line:

1. **A signal confirmed at a bar's close fills at the NEXT bar's open.**
   A live close-confirmed alert cannot trade the close print it just
   saw, so the conservative default is next-open. The optimistic
   same-close fill exists only for signal-driven exits, and the report
   labels it optimistic.
2. **A bracket is live only from the bar AFTER the fill bar.** A stop
   created by a fill cannot be hit by the fill bar itself.
3. **If one bar touches both a stop and a target, the STOP wins.**
   OHLC data cannot show the true intrabar path, so the conservative
   order is: stop legs first, then TP legs, then signal/time exits.
4. **Gap through a stop or target fills at that bar's OPEN, never at
   the untouched level.** A stop-side gap is recorded as `GapExit`;
   a TP-side gap keeps its TP reason with a gap flag.
5. **Commission is charged on EVERY fill** (entries, exits, partial
   legs). **Slippage is adverse on market/stop fills only** — never on
   TP limit fills (a limit fills at limit-or-better) and never on top
   of a gap fill (the gap itself is the adverse move).
6. **The forming/last incomplete bar is excluded** — the engine only
   ever sees closed bars.
7. **A position still open at the end of the range is force-closed at
   the last close**, flagged `force_closed` with reason `EndOfRange`.
8. **One position at a time, no pyramiding (v1).** A same-side signal
   while in a position is ignored and counted. An opposite signal while
   in a position closes (or reverses) only if opposite-exit is enabled.
9. **Margin/leverage is NOT modelled in v1.** Sizing is risk-based, not
   cash-constrained; equity may go negative in theory and is flagged as
   a warning, never hidden. A margin-call model is deferred, not faked.

## Exits

- **SL**: fixed price distance, or ATR multiple (ATR at the fill bar, so
  the stop is derived from the fill price — a fill can never cross its
  own stop).
- **TP legs**: configurable fractions at RR multiples. Fractions sum to
  at most 1.0; the remainder is the runner, which exits on a stop,
  trailing, breakeven, opposite signal, time exit, or end of range.
- **Playbook preset**: 75% at RR 3.0, 25% runner on trailing/liquidity
  exit (the owner's exit playbook: book 75% at 1:3, runner).
- **Trailing (v1 set, all ratcheted — a stop only ever moves
  favourably, expanding ATR never lowers it)**:
  - Fixed distance: stop = peak since entry minus distance.
  - Percentage: stop = peak times (1 − p).
  - ATR multiple: stop = peak minus k × ATR (ATR recomputed per bar).
  - Chandelier option: ATR trail anchored to the peak of the last N
    bars instead of since entry (set anchor to "window", N = 22 default).
  - Breakeven-after-TP1: on the TP1 fill, the stop moves to the entry
    price. This is **never labelled "risk-free"** — a gap through the
    entry still loses.
  - Activation-offset: the fixed stop holds until close-confirmed
    profit reaches the activation (in R or price units), then the
    chosen trail takes over. Close-based activation is the
    conservative, non-repainting choice.
  - Trail levels update at each bar's CLOSE from that bar's data and
    are enforced on later bars (trigger "range", the default) — or, with
    trigger "close", only closes beyond the level trigger the exit
    (reason `CloseExit`).
- **C2C ("exit on close confirmation")**: every signal-driven exit
  (opposite signal, max-bars time exit, flat-at-close exit) is
  recognised at a bar's close and fills per the timing setting:
  `next_open` (default, conservative) or `same_close` (optimistic,
  labelled). Bracket fills (stop/TP prices) are not governed by C2C —
  they fill at their level, or at the open on gaps.
- **Exit reasons** (per leg; a parent trade's final reason is its last
  leg's reason): `StopLoss`, `TakeProfit1`, `TakeProfit2`, `TrailingStop`,
  `BreakevenStop`, `CloseExit`, `OppositeSignal`, `TimeExit`,
  `EndOfRange`, `GapExit`.

## Sizing and money math (XAUUSD)

- Point = 0.01, tick value = $1.00 per lot (the Trade Taker fallback
  convention: $100 per $1.00 move per 1.0 lot, i.e. 100 oz/lot).
  P&L of a leg = direction × (exit − entry) × qty × 100.
- Fixed lot: the lot size you set. Risk-per-trade: qty = risk ÷
  (|entry − SL| value per lot), **floored to 0.01 steps, never above
  risk** — a risk too small for 0.01 lot is refused and counted as a
  skip, never rounded up.
- R-multiple: net P&L ÷ planned risk, where planned risk =
  qty × |entry − SL| value. Expectancy R is the mean R across trades.
- MT5 symbol-info overrides (live tick value / point) are a v2 route
  enhancement; v1 uses the XAUUSD fallback above and says so on the
  report. Do not run a non-XAUUSD symbol through v1 and quote the $
  figures without checking the point value first.

## Metrics

All/Long/Short splits for the headline set; drawdown on both bases:

- Net profit, gross profit/loss, profit factor (null when no losses),
  win rate, expectancy ($ and R), consecutive win/loss streaks (count
  and $), average MFE/MAE ($ and R) and the profit–MAE correlation.
- Max drawdown on **both** bases: intrabar (modelled path using each
  bar's extreme against open positions) and close-to-close (closing
  equity only). The headline is the worse of the two; both are kept,
  with peak/trough times and underwater bars.
- Recovery factor (net ÷ max drawdown $), Calmar (annualised return ÷
  max drawdown %), CAGR (only when the span is ≥ 1 day — sub-day spans
  annualise to nonsense and report null), Sharpe/Sortino on per-bar
  mark-to-market equity returns (annualised by bars-per-year for the
  timeframe, 260 trading days × 24h; risk-free rate 0).
- Buy-and-hold return over the same range, for comparison.
- Per-trade ledger: direction, signal bar, entry fill time/price, exit
  time(s)/price(s) per leg, qty per leg, SL price, TP price(s), gross
  P&L, commission, net P&L ($ and % of trade size), R-multiple, MFE/MAE
  ($ and R), duration (bars + clock time), exit reason per leg,
  cumulative net P&L after the trade.

## Robustness (all in v1)

A run is "robust" only if it passes these gates; a green headline P&L
alone proves nothing:

1. **Repaint/lookahead gate** — the provider is run on full history and
   on truncated histories; events on the shared prefix must be
   identical. Any difference refuses the run (HTTP 422, no metrics, no
   exceptions). The panel shows the badge with the truncation points
   checked.
2. **Walk-forward split view** — in-sample / out-of-sample at a 70/30
   split (positions never span slices). In-sample-only results are
   never quoted as evidence.
3. **Parameter stability** — SL/TP/trail varied ±10–20% one at a time;
   the stability share is the fraction of variants still profitable.
   A plateau is robust; a sharp peak is not.
4. **Monte Carlo reshuffle** — 1,000 paths (seed 42), closed-trade nets
   shuffled, equity rebuilt from capital; median vs P5 worst-case
   final equity and P95 max drawdown reported, plus a 10% random
   trade-skipping variant. Size live positions to survive P5, not the
   median.
5. **Cost + gap stress** — the same run at 2× and 3× commission and
   slippage. Gap-through-stop fills at the open are already in the base
   run and counted. A rule that only wins at base or zero costs is not
   an edge.

## Indicator plug-in contract

This codebase has **no discrete BUY/SELL event stream**: built-in
indicators are reference overlays that "never signal or alert", and the
Alert Engine fires on text sweeps, not typed signals. So a backtest
provider is a signal function, not a stream tap:

```python
events(bars, settings) -> (events, warmup_bars)
# events: [{"bar_index": int, "time": unix open time, "side": "BUY"|"SELL"}]
#   at most one event per bar; the event bar is the bar whose CLOSE
#   confirms the signal (the fill is the NEXT bar's open).
# warmup_bars: int — bars before which no event may be emitted.
# needs_ticks: bool — True only if the provider needs 1-second tick
#   data (then the run is refused unless the tick vault covers the range).
# htf: str | None — the higher timeframe the provider reads; HTF values
#   are usable only after the HTF bar containing bar t closes.
```

Rules every provider must satisfy (enforced by the gate, not by trust):

1. Dated BUY/SELL events, at most one per bar, with bar timestamp.
2. No event on the forming bar; an event for bar t is final only when
   bar t closes.
3. No repaint — past events never change (full-vs-truncated gate).
4. No lookahead — every value for bar t comes from bars ≤ t (no centred
   windows, no full-sample normalisation, no future-bar reads).
5. HTF values only after the containing HTF bar closes (the engine
   supplies HTF series pre-shifted to the last closed HTF bar).
6. Tick-data indicators only when the tick vault covers the range,
   else marked "tick-data required" — never run on candles as a
   substitute.
7. Warm-up honesty — the provider declares its warm-up length;
   warm-up bars emit no events and are excluded from statistics; the
   report states the warm-up length.

To add a provider: implement the function, register it in
`backtest.PROVIDERS` with a settings schema, and confirm the repaint
gate passes on a long series including gaps and regime changes. No
"trusted" bypass exists for built-ins.

## Honesty limits (read before quoting any result)

- **OHLC intrabar ambiguity**: a backtest sees open/high/low/close,
  not the true path inside the bar. SL-first, gap-at-open, and
  close-updated trails are conservative modelling choices, not
  observations. A live fill can differ.
- **Backtest ≠ live guarantee**: spreads widen at Sunday open, daily
  rollover, and news (US CPI/jobs/Fed); weekend gaps fill at the open
  by rule; multi-day holds pay/receive overnight swap (triple-swap
  Wednesdays on many brokers) — v1 does not model swaps, so exclude
  multi-day holds from a "clean" result or add a per-night swap input
  later. Never silently assume zero.
- **Spread is not modelled beyond the slippage input**: if the average
  trade is near 2× the round-trip cost, the "edge" is costs.
- **Data gaps are flagged, never interpolated**: the report states the
  data source, bar count, and history-quality %. MT5 vs OANDA
  disagreements are labelled, not averaged.
- **Costs default non-zero for a reason**: commission $0.50 per fill
  and slippage 0.10 price units. Zeroing both makes results optimistic
  and the assumptions line says so.
- **No AI in this build**: the AI take/skip scorer is a separate
  personal-only phase. This engine reports history, not predictions.
- **Sandbox results are logic proofs, not live proofs**: live MT5 data,
  canvas interaction, and any real-money behaviour are NOT verified
  here — they are the owner's PC check, DEMO first.
