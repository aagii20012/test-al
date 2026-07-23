"""Time-series (absolute) momentum.

The most robust documented anomaly in asset-pricing: an asset that has risen
over the past `lookback` bars tends to keep rising over the near term, and vice
versa (Moskowitz, Ooi & Pedersen 2012, "Time Series Momentum").

Rule: compute the rate of change over `lookback` bars. Go LONG if it exceeds
`+threshold`, SHORT if below `-threshold` (when shorting is allowed), and EXIT
into the neutral band. Conviction (`strength`) scales with the magnitude of the
move so the RiskManager sizes stronger trends larger.

A volatility-normalised variant divides the raw return by recent realised
volatility (`vol_norm=True`), which stabilises position sizing across regimes —
the standard way real CTAs target constant risk rather than constant notional.
"""

from __future__ import annotations

import numpy as np

from ..core.enums import SignalType
from ..core.events import MarketEvent, SignalEvent
from .base import Strategy


class MomentumStrategy(Strategy):
    def __init__(self, data, events, lookback=48, threshold=0.0, exit_band=0.0,
                 vol_norm=True, allow_short=True, **kw):
        super().__init__(data, events, lookback=lookback, threshold=threshold,
                         exit_band=exit_band, vol_norm=vol_norm,
                         allow_short=allow_short, **kw)
        self.lookback = int(lookback)
        # threshold/exit_band are expressed as raw return (or vol-units if vol_norm)
        self.threshold = float(threshold)
        self.exit_band = float(exit_band)
        self.vol_norm = bool(vol_norm)
        self.allow_short = bool(allow_short)
        self._pos = {s: 0 for s in self.symbols}

    def _score(self, closes: np.ndarray) -> float:
        raw = closes[-1] / closes[0] - 1.0
        if not self.vol_norm:
            return raw
        rets = np.diff(closes) / closes[:-1]
        vol = rets.std(ddof=0)
        if vol <= 0:
            return 0.0
        # Annualisation cancels out for a pure ranking score; keep it scale-free.
        return raw / (vol * np.sqrt(self.lookback))

    def calculate_signals(self, event: MarketEvent) -> None:
        n = self.lookback + 1
        for symbol in self.symbols:
            closes = self.closes(symbol, n)
            if len(closes) < n:
                continue

            score = self._score(closes)
            bar = self.data.get_latest_bar(symbol)
            state = self._pos[symbol]
            strength = float(min(1.0, abs(score) / (self.threshold * 3) if self.threshold > 0 else 1.0))
            strength = max(0.1, strength)

            if state == 0:
                if score > self.threshold:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.LONG, strength))
                    self._pos[symbol] = 1
                elif self.allow_short and score < -self.threshold:
                    self.events.put(SignalEvent(symbol, bar.dt, SignalType.SHORT, strength))
                    self._pos[symbol] = -1
            elif state > 0 and score <= self.exit_band:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0
            elif state < 0 and score >= -self.exit_band:
                self.events.put(SignalEvent(symbol, bar.dt, SignalType.EXIT))
                self._pos[symbol] = 0

    # ---- state persistence (for the run-once / cloud "tick" mode) --------
    def dump_state(self) -> dict:
        return {"pos": dict(self._pos)}

    def load_state(self, state: dict) -> None:
        saved = state.get("pos", {}) if state else {}
        self._pos = {s: int(saved.get(s, 0)) for s in self.symbols}

    # sync_positions is inherited from Strategy: the generic base reconciles
    # `_pos` from the portfolio (long -> 1, short -> -1, flat -> 0), so momentum
    # obeys the same portfolio-authoritative contract as every other strategy.
