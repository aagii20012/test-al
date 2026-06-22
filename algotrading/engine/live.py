"""Live engine: drive the shared loop over an open-ended real-time stream.

Identical dispatch to the backtester; the only differences are:
  * the loop runs until interrupted (Ctrl-C) rather than until data is exhausted,
  * `data.update_bars()` blocks for the next closed bar instead of reading a row,
  * a graceful shutdown flushes a final mark-to-market.
"""

from __future__ import annotations

import signal as _signal

from ..core.event_queue import EventQueue
from ..utils.logger import get_logger
from .loop import dispatch_pending

log = get_logger(__name__)


class LiveEngine:
    def __init__(self, data, strategy, portfolio, execution, events: EventQueue):
        self.data = data
        self.strategy = strategy
        self.portfolio = portfolio
        self.execution = execution
        self.events = events
        self._running = True

    def _handle_sigint(self, *_):
        log.warning("Shutdown requested; finishing current cycle...")
        self._running = False
        if hasattr(self.data, "stop"):
            self.data.stop()

    def run(self):
        _signal.signal(_signal.SIGINT, self._handle_sigint)
        log.info("Live trading started on %s. Ctrl-C to stop.", list(self.data.symbols))
        try:
            while self._running and self.data.continue_trading:
                self.data.update_bars()
                dispatch_pending(self.events, self.strategy, self.portfolio, self.execution)
                # Per-bar pulse: show the decision outcome even when no trade fires.
                for sym in self.data.symbols:
                    bar = self.data.get_latest_bar(sym)
                    if bar is not None:
                        log.info("tick %s close=%.2f | position=%s | equity=%.2f",
                                 sym, bar.close, self.portfolio.position(sym),
                                 self.portfolio.equity)
        finally:
            log.info("Live session ended. Final equity (mark-to-market): %.2f",
                     self.portfolio.equity)
        return self.portfolio
