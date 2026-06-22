"""Backtest engine: drive the shared loop over a finite historical stream."""

from __future__ import annotations

from ..core.event_queue import EventQueue
from ..utils.logger import get_logger
from .loop import dispatch_pending

log = get_logger(__name__)


class BacktestEngine:
    def __init__(self, data, strategy, portfolio, execution, events: EventQueue):
        self.data = data
        self.strategy = strategy
        self.portfolio = portfolio
        self.execution = execution
        self.events = events

    def run(self):
        log.info("Starting backtest over %d symbols", len(self.data.symbols))
        steps = 0
        while self.data.continue_trading:
            self.data.update_bars()            # emits a MarketEvent (or ends)
            dispatch_pending(self.events, self.strategy, self.portfolio, self.execution)
            steps += 1
        log.info("Backtest complete: %d steps, final equity %.2f",
                 steps, self.portfolio.equity)
        return self.portfolio
