"""Donchian-channel breakout — a classic breakout / trend-capture system.

Goes LONG when price closes above the highest high of the last `entry` bars,
goes SHORT (if `allow_short`) when it closes below the lowest low, and EXITs
when price crosses back through the opposite, shorter `exit` channel. This is the
core of the original "Turtle" rules: enter on new extremes, ride the trend, and
let an inner channel trail the exit.

A long trend filter (`trend`) optionally suppresses counter-trend entries: only
take longs above the trend SMA and shorts below it. This cuts the whipsaws that
plague raw breakout systems in ranging markets.
"""

from __future__ import annotations

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


class DonchianBreakoutStrategy(Strategy):
    def __init__(self, data, events, entry=55, exit=20, trend=0, allow_short=True, **kw):
        super().__init__(data, events, entry=entry, exit=exit, trend=trend,
                         allow_short=allow_short, **kw)
        self.entry = int(entry)
        self.exit = int(exit)
        self.trend = int(trend)          # 0 disables the trend filter
        self.allow_short = bool(allow_short)
        # +1 long, -1 short, 0 flat
        self._pos = {s: 0 for s in self.symbols}

    def calculate_signals(self, event: MarketEvent) -> None:
        lookback = max(self.entry, self.exit, self.trend) + 1
        for symbol in self.symbols:
            bars = self.data.get_latest_bars(symbol, lookback)
            if len(bars) < lookback:
                continue

            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            bar = bars[-1]
            price = bar.close

            # Channels exclude the current bar (use prior `n` bars).
            upper = max(highs[-self.entry - 1:-1])
            lower = min(lows[-self.entry - 1:-1])
            exit_low = min(lows[-self.exit - 1:-1])
            exit_high = max(highs[-self.exit - 1:-1])

            trend_ok_long = trend_ok_short = True
            if self.trend > 0:
                sma = sum(b.close for b in bars[-self.trend:]) / self.trend
                trend_ok_long = price >= sma
                trend_ok_short = price <= sma

            state = self._pos[symbol]

            # --- exits first (inner channel) ---------------------------------
            if state > 0 and price <= exit_low:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0
            elif state < 0 and price >= exit_high:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0

            # --- entries (outer channel breakout) ----------------------------
            if state == 0:
                if price > upper and trend_ok_long:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG))
                    self._pos[symbol] = 1
                elif self.allow_short and price < lower and trend_ok_short:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.SHORT))
                    self._pos[symbol] = -1
