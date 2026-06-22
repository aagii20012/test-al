"""Buy-and-hold benchmark: go LONG on the first bar, never exit.

Useful as a baseline to judge whether an active strategy actually adds value.
"""

from __future__ import annotations

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


class BuyAndHoldStrategy(Strategy):
    def __init__(self, data, events, **kw):
        super().__init__(data, events, **kw)
        self._bought = {s: False for s in self.symbols}

    def calculate_signals(self, event: MarketEvent) -> None:
        for symbol in self.symbols:
            if self._bought[symbol]:
                continue
            bar = self.data.get_latest_bar(symbol)
            if bar is None:
                continue
            self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG))
            self._bought[symbol] = True
