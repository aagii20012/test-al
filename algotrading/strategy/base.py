"""Strategy abstraction — the plug-and-play extension point.

A strategy consumes MarketEvents and emits SignalEvents. It is intentionally
ignorant of position sizing, risk, execution, and accounting; those belong to
the portfolio/risk/execution layers. This separation is what makes strategies
small, testable, and swappable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..core.event_queue import EventQueue
from ..core.events import MarketEvent
from ..data.base import DataHandler


class Strategy(ABC):
    def __init__(self, data: DataHandler, events: EventQueue, **params):
        self.data = data
        self.events = events
        self.symbols: List[str] = list(data.symbols)
        self.params = params

    @abstractmethod
    def calculate_signals(self, event: MarketEvent) -> None:
        """Inspect the latest market data and put SignalEvents on the queue."""

    # ---- state persistence (for the run-once / cloud "tick" mode) --------
    # Strategies that carry memory between bars (e.g. a long/short/flat flag)
    # override these so a process that restarts each cycle resumes correctly.
    # Stateless strategies (recomputed purely from bars) need no override.
    def dump_state(self) -> dict:
        return {}

    def load_state(self, state: dict) -> None:
        return None

    def sync_positions(self, portfolio) -> None:
        """Reconcile internal position memory with the ACTUAL book.

        A risk-driven stop or circuit-breaker flatten, or an order that was
        vetoed / rounded to zero / rejected, closes (or never opens) the real
        position without the strategy knowing. Trusting stale memory would wedge
        the strategy out of the market — so before each decision the real
        position is treated as the single source of truth.

        This generic implementation overwrites whichever position memory a
        strategy keeps directly from the portfolio, so every stateful strategy
        obeys the same reconciliation contract without its own override:

          * ``_pos``       — signed direction, set to -1 / 0 / +1
          * ``_in_market`` — boolean, set to (position != 0)

        Both are *derived* from the book here; neither is an independent
        authoritative store. Truly stateless strategies keep neither attribute,
        so this is a no-op for them.
        """
        has_pos = hasattr(self, "_pos")
        has_flag = hasattr(self, "_in_market")
        if not (has_pos or has_flag):
            return
        for s in self.symbols:
            pos = portfolio.position(s)
            if has_pos:
                self._pos[s] = 1 if pos > 0 else (-1 if pos < 0 else 0)
            if has_flag:
                self._in_market[s] = pos != 0

    # Convenience: strategies often want a numpy array of recent closes.
    def closes(self, symbol: str, n: int):
        import numpy as np

        bars = self.data.get_latest_bars(symbol, n)
        return np.array([b.close for b in bars], dtype=float)
