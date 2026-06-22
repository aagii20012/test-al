"""Simple moving-average crossover.

Goes LONG when the fast SMA crosses above the slow SMA, EXITs when it crosses
back below. A textbook trend-following example demonstrating the strategy
contract; not a recommendation.
"""

from __future__ import annotations

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


class SMACrossoverStrategy(Strategy):
    def __init__(self, data, events, fast=20, slow=50, **kw):
        super().__init__(data, events, fast=fast, slow=slow, **kw)
        self.fast = int(fast)
        self.slow = int(slow)
        if self.fast >= self.slow:
            raise ValueError("fast window must be < slow window")
        self._in_market = {s: False for s in self.symbols}

    def calculate_signals(self, event: MarketEvent) -> None:
        for symbol in self.symbols:
            closes = self.closes(symbol, self.slow)
            if len(closes) < self.slow:
                continue

            fast_ma = closes[-self.fast:].mean()
            slow_ma = closes.mean()
            bar = self.data.get_latest_bar(symbol)

            if fast_ma > slow_ma and not self._in_market[symbol]:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG))
                self._in_market[symbol] = True
            elif fast_ma < slow_ma and self._in_market[symbol]:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._in_market[symbol] = False
