"""Live execution via an Exchange implementation.

Submits market orders and converts the exchange's response into a FillEvent.
Quantity rounding to the exchange lot-size filter is handled inside the Exchange
adapter so this layer stays venue-agnostic.
"""

from __future__ import annotations

from ..core.events import FillEvent, OrderEvent
from ..core.event_queue import EventQueue
from ..utils.logger import get_logger
from .base import ExecutionHandler

log = get_logger(__name__)


class LiveExecutionHandler(ExecutionHandler):
    def __init__(self, events: EventQueue, exchange):
        self.events = events
        self.exchange = exchange

    def execute_order(self, order: OrderEvent) -> None:
        try:
            fill = self.exchange.place_market_order(
                symbol=order.symbol,
                direction=order.direction,
                quantity=order.quantity,
            )
        except Exception as exc:  # noqa: BLE001 - never let one bad order kill the loop
            log.error("Order failed for %s: %s", order.symbol, exc)
            return

        if fill is not None:
            self.events.put(fill)
