"""Enumerations shared across the framework."""

from enum import Enum


class EventType(str, Enum):
    MARKET = "MARKET"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"
    FILL = "FILL"


class SignalType(str, Enum):
    """A strategy's directional intent (exchange-agnostic)."""

    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"  # close any open position in the symbol


class Direction(str, Enum):
    """Side of an order/fill."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
