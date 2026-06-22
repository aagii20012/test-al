"""Bollinger-band mean reversion (z-score reversion).

Mean-reversion counterpart to the trend systems: when price stretches far from
its rolling mean it tends to snap back. We measure "far" with a z-score —
(price - SMA) / rolling_std — which is unit-free and adapts to volatility.

Rule: go LONG when z falls below `-entry_z` (oversold), SHORT (if allowed) when z
rises above `+entry_z`, and EXIT when z reverts past `exit_z` toward the mean.
Conviction scales with how stretched the band is.

An optional regime filter (`trend`) only takes reversion trades in the direction
of the longer trend, because buying dips in a downtrend ("catching a falling
knife") is how naive mean-reversion blows up. With `trend=0` it reverts freely.
"""

from __future__ import annotations

import numpy as np

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


class BollingerReversionStrategy(Strategy):
    def __init__(self, data, events, window=20, entry_z=2.0, exit_z=0.5,
                 trend=0, allow_short=True, **kw):
        super().__init__(data, events, window=window, entry_z=entry_z,
                         exit_z=exit_z, trend=trend, allow_short=allow_short, **kw)
        self.window = int(window)
        self.entry_z = float(entry_z)
        self.exit_z = float(exit_z)
        self.trend = int(trend)
        self.allow_short = bool(allow_short)
        self._pos = {s: 0 for s in self.symbols}

    def calculate_signals(self, event: MarketEvent) -> None:
        n = max(self.window, self.trend) + 1
        for symbol in self.symbols:
            closes = self.closes(symbol, n)
            if len(closes) < self.window + 1:
                continue

            win = closes[-self.window:]
            mean = win.mean()
            std = win.std(ddof=0)
            if std <= 0:
                continue
            z = (closes[-1] - mean) / std
            bar = self.data.get_latest_bar(symbol)
            state = self._pos[symbol]

            trend_up = trend_dn = True
            if self.trend > 0 and len(closes) >= self.trend:
                tsma = closes[-self.trend:].mean()
                trend_up = closes[-1] >= tsma     # only buy dips in an uptrend
                trend_dn = closes[-1] <= tsma     # only fade rips in a downtrend

            strength = float(min(1.0, max(0.1, (abs(z) - self.entry_z) / self.entry_z + 0.3)))

            # --- exits: reverted back toward the mean ------------------------
            if state > 0 and z >= -self.exit_z:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0
            elif state < 0 and z <= self.exit_z:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0

            # --- entries: stretched away from the mean -----------------------
            if state == 0:
                if z < -self.entry_z and trend_up:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG, strength))
                    self._pos[symbol] = 1
                elif self.allow_short and z > self.entry_z and trend_dn:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.SHORT, strength))
                    self._pos[symbol] = -1
