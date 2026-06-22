"""Volatility-based strategy: Keltner-channel squeeze breakout.

Volatility is mean-reverting at the *level* of volatility itself: quiet periods
("squeezes") are followed by violent expansions. This strategy waits for a
low-volatility squeeze, then trades the direction of the expansion when price
breaks the Keltner channel — the classic "TTM squeeze" volatility play.

Definitions (all on the trailing window, no look-ahead):
  * Keltner channel : EMA(close, n) +/- kc_mult * ATR(n)
  * Bollinger band  : SMA(close, n) +/- bb_mult * stdev(close, n)
  * squeeze ON      : Bollinger band sits *inside* the Keltner channel
                      (volatility compressed). The release of a squeeze is the
                      setup; the breakout direction is the trade.

Entry : after a recent squeeze, go LONG on a close above the Keltner upper band,
        SHORT (if allowed) on a close below the lower band.
Exit  : price reverts to the channel mid (the EMA), or flips to the far band.

This complements the directional families: it is agnostic to trend and only
acts when volatility regime change creates an edge, then steps aside.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict

import numpy as np

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


def _ema(values: np.ndarray, period: int) -> float:
    """Exponential moving average of the trailing window (last value)."""
    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _atr_from_bars(bars, period: int) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return float("nan")
    return float(np.mean(trs[-period:]))


class VolatilityBreakoutStrategy(Strategy):
    def __init__(self, data, events, window=20, kc_mult=1.5, bb_mult=2.0,
                 atr_period=14, squeeze_lookback=6, use_squeeze=True,
                 allow_short=True, **kw):
        super().__init__(data, events, window=window, kc_mult=kc_mult,
                         bb_mult=bb_mult, atr_period=atr_period,
                         squeeze_lookback=squeeze_lookback, use_squeeze=use_squeeze,
                         allow_short=allow_short, **kw)
        self.window = int(window)
        self.kc_mult = float(kc_mult)
        self.bb_mult = float(bb_mult)
        self.atr_period = int(atr_period)
        self.squeeze_lookback = int(squeeze_lookback)
        self.use_squeeze = bool(use_squeeze)
        self.allow_short = bool(allow_short)
        self._pos: Dict[str, int] = {s: 0 for s in self.symbols}
        self._squeeze_hist: Dict[str, Deque[bool]] = {
            s: deque(maxlen=self.squeeze_lookback) for s in self.symbols
        }

    def calculate_signals(self, event: MarketEvent) -> None:
        need = max(self.window, self.atr_period) + 2
        for symbol in self.symbols:
            bars = self.data.get_latest_bars(symbol, need)
            if len(bars) < need:
                continue
            closes = np.array([b.close for b in bars], dtype=float)
            win = closes[-self.window:]

            mid = _ema(closes[-self.window:], self.window)
            atr = _atr_from_bars(bars, self.atr_period)
            if not np.isfinite(atr) or atr <= 0:
                continue
            sma = win.mean()
            std = win.std(ddof=0)

            kc_up, kc_lo = mid + self.kc_mult * atr, mid - self.kc_mult * atr
            bb_up, bb_lo = sma + self.bb_mult * std, sma - self.bb_mult * std

            squeeze_on = (bb_up < kc_up) and (bb_lo > kc_lo)
            self._squeeze_hist[symbol].append(squeeze_on)
            recent_squeeze = any(self._squeeze_hist[symbol])

            price = closes[-1]
            bar = bars[-1]
            state = self._pos[symbol]

            # --- exits: reverted to the channel mid -------------------------
            if state > 0 and price <= mid:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0
            elif state < 0 and price >= mid:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
                state = 0

            # --- entries: volatility expansion out of a squeeze -------------
            if state == 0:
                gated = recent_squeeze or not self.use_squeeze
                # Conviction scales with how far price cleared the band, in ATRs.
                if price > kc_up and gated:
                    strength = float(min(1.0, max(0.2, (price - kc_up) / atr)))
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG, strength))
                    self._pos[symbol] = 1
                elif self.allow_short and price < kc_lo and gated:
                    strength = float(min(1.0, max(0.2, (kc_lo - price) / atr)))
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.SHORT, strength))
                    self._pos[symbol] = -1
