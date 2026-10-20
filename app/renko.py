"""
Traditional Renko brick builder.

Builds wickless Renko bricks from OHLC bars (M1), standard "Traditional"
Renko rules:
  - fixed box size (default 2.5 price units for XAUUSD)
  - a new brick closes only when price moves a FULL box from the current anchor
  - penetration is tested against bar high/low, so one bar can close several bricks
  - reversal also needs a full box (symmetric, like TV traditional)

Each brick is returned wickless: high/low are set to the brick body edges.

the chain is the ONLY writer of bricks. It owns the live edge too:
completed 1-second bars from the local tick vault are consumed in tick order
(update_ticks), so the chart, Renko-pane indicators and the background Alert
Engine all read one identical, tick-true series. The old per-second tracker
kept a SECOND anchor and its bricks were wiped by every chart poll and then
re-emitted - the last leg of the chart blinked. That tracker is gone.
"""

import time

BRICK_TIME_DEDUP_STEP_S = 1  # seconds added when two bricks share a timestamp

# Safety cap: a chain never holds more than this many closed bricks
# (20000 M1 bars realistically yield a few thousand XAUUSD bricks).
MAX_CHAIN_BRICKS = 100000

# Bound on 1-second catch-up per call (2h of seconds; chains are touched every
# few seconds while in use, so this is only a safety valve).
MAX_TICK_CATCHUP_S = 7200


class RenkoChain:
    """Incremental Traditional Renko builder with stable history.

    A full rebuild re-anchors the ENTIRE brick series from bar 0, so a single
    revised M1 bar (tick-vault healing vs MT5 recovery routinely revise the
    tail) shifts every later brick - and any overlay drawn on those bricks
    flickers ("coming and going"). A closed Renko brick is never revised,
    so neither do we: once a brick closes it is immutable; new bars
    only APPEND. Interior revisions of the M1 feed are ignored for bricks.

    The forming brick is ephemeral and rebuilt from the live price on every
    read; it is never stored in the chain.

    the chain also owns the live edge. Completed 1-second bars from
    the tick vault are consumed in time order (update_ticks), so bricks close
    from real tick order at 1-second resolution instead of a base bar's
    (low, high, close) path assumption. There is exactly one brick series per
    (symbol, box, base_tf): the chart, Renko-pane indicators and background
    alerts all read this chain - no second tracker that can diverge from it.
    """

    def __init__(self, box_size, sec=60):
        self.box = float(box_size)
        self.sec = int(sec or 60)  # base-bar length in seconds
        self.anchor = None     # open price the next brick is measured from
        self.last_bar_t = 0    # max base-bar time consumed so far
        self.last_tick_t = 0   # newest completed 1-second bar consumed
        self.tick_from = 0     # oldest second the tick path owns
        self._last_ts = 0      # brick-time dedup cursor
        self.bricks = []       # closed bricks only, chronological

    def _push(self, ts, b_open, b_close):
        ts = int(ts)
        if self.bricks and ts <= self._last_ts:
            ts = self._last_ts + BRICK_TIME_DEDUP_STEP_S
        self._last_ts = ts
        up = b_close > b_open
        self.bricks.append({
            "time": ts,
            "open": round(b_open, 3),
            "high": round(max(b_open, b_close), 3),
            "low": round(min(b_open, b_close), 3),
            "close": round(b_close, 3),
            "forming": False,
            "dir": 1 if up else -1,
        })
        if len(self.bricks) > MAX_CHAIN_BRICKS:
            # Drop the oldest; the anchor already encodes everything needed
            # to continue the chain, so future bricks are unaffected.
            del self.bricks[:len(self.bricks) - MAX_CHAIN_BRICKS]

    def _process_bar(self, b):
        box = self.box
        anchor = self.anchor
        o = float(b["open"])
        h = float(b["high"])
        l = float(b["low"])
        c = float(b["close"])
        # Intrabar path assumption (standard OHLC->Renko convention):
        # a bar that closes up most likely travelled low -> high -> close,
        # a bar that closes down travelled high -> low -> close.
        # Each leg is monotonic, so brick generation per leg always terminates.
        extremes = (l, h, c) if c >= o else (h, l, c)
        for e in extremes:
            if e >= anchor + box:
                while e >= anchor + box:
                    new_close = anchor + box
                    self._push(b["time"], anchor, new_close)
                    anchor = new_close
            elif e <= anchor - box:
                while e <= anchor - box:
                    new_close = anchor - box
                    self._push(b["time"], anchor, new_close)
                    anchor = new_close
        self.anchor = anchor

    def rebuild(self, bars, now=None):
        """Full rebuild from all bars (first build or box change).

        the still-forming base bar is never consumed - consuming it
        partially and then skipping it at completion (same timestamp) used
        to permanently lose the bar's late range.
        """
        now = now or time.time()
        bars = list(bars or [])
        self.bricks = []
        self.anchor = None
        self.last_bar_t = 0
        self._last_ts = 0
        if not bars or self.box <= 0:
            return
        # anchor = open price of the next brick (== close of the previous).
        # No direction memory: a new brick closes whenever price moves a full
        # box from the anchor in EITHER direction (TV traditional Renko).
        self.anchor = float(bars[0]["close"])
        self._last_ts = int(bars[0]["time"])
        for b in bars[1:]:
            bt = int(b["time"])
            if bt + self.sec > now:
                break  # forming bucket: the tick path owns it
            self._process_bar(b)
            self.last_bar_t = bt

    def update_ticks(self, secs, now=None):
        """Consume completed 1-second bars (tick-vault truth) in time order.

        This is the live edge: bricks close from real tick order at 1-second
        resolution. Only COMPLETED seconds are consumed - the still-open
        second is left for the ephemeral forming brick. Every consumer
        (chart poll, per-second quote poll, indicators, Alert Engine) shares
        the one resulting series, so the last leg can never disagree with
        itself.
        """
        if self.anchor is None:
            return 0  # no history to anchor on; bars must seed us first
        now_sec = int(now or time.time())
        n = 0
        for s in secs or []:
            t = int(s["time"])
            if t <= self.last_tick_t or t >= now_sec:
                continue
            if not self.tick_from or t < self.tick_from:
                self.tick_from = t
            self._process_bar(s)
            self.last_tick_t = t
            n += 1
            if n >= MAX_TICK_CATCHUP_S:
                break
        return n

    def update(self, bars, now=None):
        """Append bricks for base bars newer than what we consumed.

        Closed bricks are immutable; interior revisions are ignored (as
        before).
        - the still-forming base bar is NEVER consumed (see rebuild).
        - a completed bar fully covered by the tick path (its seconds were
          already consumed by update_ticks, in true tick order) is skipped:
          its bricks are already exact.
        Reprocessing a bar is always safe - the anchor only advances, so a
        bar can only close bricks for range beyond it, never duplicates.
        """
        if self.anchor is None:
            self.rebuild(bars, now)
            return
        now = now or time.time()
        for b in bars or []:
            bt = int(b["time"])
            if bt <= self.last_bar_t:
                continue
            bar_end = bt + self.sec
            if bar_end > now:
                break  # forming bucket: never consume (see rebuild)
            if (self.tick_from and bt >= self.tick_from
                    and bar_end - 1 <= self.last_tick_t):
                self.last_bar_t = bt  # tick-covered: bricks already exact
                continue
            self._process_bar(b)
            self.last_bar_t = bt

    def with_forming(self, live_price=None):
        """Closed bricks plus the ephemeral forming brick from the live
        price (what TV draws as the unfinished brick)."""
        out = list(self.bricks)
        if live_price is not None and self.anchor is not None:
            lp = float(live_price)
            box = self.box
            anchor = self.anchor
            if lp > anchor:
                f_close = min(lp, anchor + box)
            elif lp < anchor:
                f_close = max(lp, anchor - box)
            else:
                f_close = anchor
            if abs(f_close - anchor) > 1e-9:
                ts = self._last_ts
                if out and ts <= self._last_ts:
                    ts = self._last_ts + BRICK_TIME_DEDUP_STEP_S
                out.append({
                    "time": ts,
                    "open": round(anchor, 3),
                    "high": round(max(anchor, f_close), 3),
                    "low": round(min(anchor, f_close), 3),
                    "close": round(f_close, 3),
                    "forming": True,
                    "dir": 1 if f_close > anchor else -1,
                })
        return out


def build_renko(bars, box_size, live_price=None):
    """
    bars: iterable of dicts with keys time (unix seconds), open, high, low, close
          in chronological order.
    box_size: brick size in price units (e.g. 2.5)
    live_price: optional latest tick price -> appends a "forming" brick so the
                chart shows the in-progress move, like standard Renko charts do.

    Returns (bricks, anchor) where bricks is a list of
    {time, open, high, low, close, forming} and anchor is the open price the
    next brick will be measured from.
    """
    chain = RenkoChain(box_size)
    chain.rebuild(bars)
    bricks = chain.with_forming(live_price)
    return bricks, chain.anchor
