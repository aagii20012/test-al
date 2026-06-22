"""The single canonical event-dispatch loop, shared by both engines.

This function is the structural guarantee that backtesting and live trading
behave identically: there is exactly one implementation of "what happens when an
event of type X appears". The engines differ only in how `data.update_bars()` is
driven (a finite loop over history vs. an open-ended poll).
"""

from __future__ import annotations

from ..core.enums import EventType
from ..core.event_queue import EventQueue
from ..core.events import FillEvent, MarketEvent, OrderEvent, SignalEvent


def dispatch_pending(events: EventQueue, strategy, portfolio, execution) -> None:
    """Drain and route every event currently on the queue.

    Note new events may be enqueued while draining (a MarketEvent spawns a
    SignalEvent spawns an OrderEvent spawns a FillEvent); `drain()` keeps going
    until the queue is genuinely empty.
    """
    for event in events.drain():
        if event.type is EventType.MARKET:
            assert isinstance(event, MarketEvent)
            # 1. Fill any orders deferred from a prior bar (latency) against this
            #    newly-arrived bar and apply them NOW, so the bar's equity mark
            #    reflects positions actually held. No-op for immediate-fill
            #    handlers (they return no deferred fills).
            for fill in execution.on_market(event):
                portfolio.update_fill(fill)
            # 2. Strategy reacts to the new bar; 3. portfolio marks-to-market.
            strategy.calculate_signals(event)
            portfolio.update_timeindex(event)
        elif event.type is EventType.SIGNAL:
            assert isinstance(event, SignalEvent)
            portfolio.update_signal(event)
        elif event.type is EventType.ORDER:
            assert isinstance(event, OrderEvent)
            execution.execute_order(event)
        elif event.type is EventType.FILL:
            assert isinstance(event, FillEvent)
            portfolio.update_fill(event)
