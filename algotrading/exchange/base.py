"""Exchange abstraction.

Defines the minimal contract the rest of the framework needs from a venue:
  * fetch_ohlcv  — historical/recent candles as a DataFrame,
  * place_market_order — submit an order, return a FillEvent,
  * account_balances — for reconciliation / sanity checks.

Implement this once per venue (Binance provided). Execution handlers depend on
this interface, not on any vendor SDK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

import pandas as pd

from ..core.enums import Direction
from ..core.events import FillEvent


class Exchange(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_ohlcv(
        self, symbol: str, interval: str = "1h", limit: int = 500, days: Optional[int] = None
    ) -> pd.DataFrame:
        """Return OHLCV indexed by open time (UTC), columns o/h/l/c/volume."""

    @abstractmethod
    def place_market_order(
        self, symbol: str, direction: Direction, quantity: float
    ) -> Optional[FillEvent]:
        ...

    @abstractmethod
    def account_balances(self) -> Dict[str, float]:
        ...
