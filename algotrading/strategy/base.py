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
        position is treated as the source of truth. Stateless strategies need no
        override."""
        return None

    # Convenience: strategies often want a numpy array of recent closes.
    def closes(self, symbol: str, n: int):
        import numpy as np

        bars = self.data.get_latest_bars(symbol, n)
        return np.array([b.close for b in bars], dtype=float)
