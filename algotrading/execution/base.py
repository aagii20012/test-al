"""ExecutionHandler abstraction.

Takes OrderEvents and produces FillEvents. The simulated handler models fills
against historical bars; the live handler submits to a real exchange. Strategies
and the portfolio never know which one is in play.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.events import OrderEvent


class ExecutionHandler(ABC):
    @abstractmethod
    def execute_order(self, order: OrderEvent) -> None:
        """Execute `order` and put the resulting FillEvent on the queue."""

    def on_market(self, event) -> list:
        """Hook fired once per MarketEvent, before strategy signals.

        Lets an execution model that defers fills (e.g. latency: decide on bar
        N's close, fill at bar N+1's open) flush its working orders against the
        newly-arrived bar. It returns the resulting FillEvents so the engine can
        apply them to the portfolio *before* marking-to-market, keeping the
        equity curve consistent with the positions actually held that bar.

        The default returns an empty list, so simple/live handlers that fill
        immediately are unaffected and backtest/live parity is preserved.
        """
        return []
