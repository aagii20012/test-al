"""Event objects that flow through the system.

The whole framework is decoupled through these four events. A producer never
calls a consumer directly; it puts an event on the queue and the engine loop
dispatches it. This is what lets backtest and live share one control flow.

`type` is a ClassVar (not an init field) so it never interferes with dataclass
field-ordering across inheritance, while `event.type` still works for dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Optional

from .enums import Direction, EventType, OrderType, SignalType


@dataclass
class Event:
    type: ClassVar[EventType]


@dataclass
class MarketEvent(Event):
    """A new bar (or batch of bars) is available from the DataHandler."""

    dt: datetime
    type: ClassVar[EventType] = EventType.MARKET


@dataclass
class SignalEvent(Event):
    """A Strategy's directional view on a symbol.

    `strength` lets a strategy express conviction in [0, 1]; the RiskManager may
    use it to scale position size.
    """

    symbol: str
    dt: datetime
    signal_type: SignalType
    strength: float = 1.0
    type: ClassVar[EventType] = EventType.SIGNAL


@dataclass
class OrderEvent(Event):
    """A sized, risk-approved instruction destined for an exchange."""

    symbol: str
    order_type: OrderType
    quantity: float
    direction: Direction
    price: Optional[float] = None  # required for LIMIT orders
    type: ClassVar[EventType] = EventType.ORDER

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"OrderEvent quantity must be > 0, got {self.quantity}")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT order requires a price")


@dataclass
class FillEvent(Event):
    """A confirmed execution reported back by the ExecutionHandler."""

    dt: datetime
    symbol: str
    direction: Direction
    quantity: float
    fill_price: float
    commission: float = 0.0
    # Dollar cost of slippage + latency already embedded in `fill_price` (vs the
    # un-slipped reference price). Carried for cost attribution only — it must
    # NOT be debited from cash again (it is already in fill_price).
    slippage_cost: float = 0.0
    exchange: str = "SIM"
    type: ClassVar[EventType] = EventType.FILL

    @property
    def gross_value(self) -> float:
        return self.quantity * self.fill_price
