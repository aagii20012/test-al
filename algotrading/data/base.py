"""DataHandler abstraction.

A DataHandler is responsible for delivering bars to the rest of the system, one
time-step at a time, via MarketEvents. Strategies and the portfolio only ever
ask for "the latest N bars", never for the whole series. This restriction is
what prevents look-ahead bias: a strategy structurally cannot see the future.

Backtest and live differ only in *where* bars come from and *what drives the
clock* — the interface is identical.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import namedtuple
from typing import List, Sequence

# Canonical OHLCV bar used everywhere downstream.
Bar = namedtuple("Bar", ["symbol", "dt", "open", "high", "low", "close", "volume"])


class DataHandler(ABC):
    symbols: Sequence[str]

    @abstractmethod
    def get_latest_bars(self, symbol: str, n: int = 1) -> List[Bar]:
        """Return the last `n` bars for `symbol` (oldest first).

        Returns fewer than `n` if not enough history has streamed yet.
        """

    @abstractmethod
    def get_latest_bar(self, symbol: str) -> Bar | None:
        """Return the most recent bar for `symbol`, or None."""

    @abstractmethod
    def update_bars(self) -> None:
        """Advance the clock by one step and push a MarketEvent.

        In backtest this reads the next row; in live it fetches/awaits the next
        closed bar.
        """

    @property
    @abstractmethod
    def continue_trading(self) -> bool:
        """False when the data stream is exhausted (backtest end / shutdown)."""
