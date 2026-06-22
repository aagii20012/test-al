"""RSI mean-reversion strategy.

LONG when RSI drops below `oversold`, EXIT when it recovers above `exit_level`.
"""

from __future__ import annotations

import numpy as np

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


def rsi(closes: np.ndarray, period: int) -> float:
    """Wilder's RSI on the trailing window."""
    deltas = np.diff(closes)
    if len(deltas) < period:
        return 50.0
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class RSIStrategy(Strategy):
    def __init__(self, data, events, period=14, oversold=30, exit_level=50, **kw):
        super().__init__(data, events, period=period, oversold=oversold,
                         exit_level=exit_level, **kw)
        self.period = int(period)
        self.oversold = float(oversold)
        self.exit_level = float(exit_level)
        self._in_market = {s: False for s in self.symbols}

    def calculate_signals(self, event: MarketEvent) -> None:
        for symbol in self.symbols:
            closes = self.closes(symbol, self.period + 1)
            if len(closes) < self.period + 1:
                continue

            value = rsi(closes, self.period)
            bar = self.data.get_latest_bar(symbol)

            if value < self.oversold and not self._in_market[symbol]:
                strength = min(1.0, (self.oversold - value) / self.oversold)
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG, strength))
                self._in_market[symbol] = True
            elif value > self.exit_level and self._in_market[symbol]:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._in_market[symbol] = False
