"""A thin FIFO wrapper used as the system's single event bus.

Kept as a separate type so we can later swap in a priority queue (e.g. to model
event latency) without touching producers/consumers.
"""

from __future__ import annotations

import queue
from typing import Iterator, Optional

from .events import Event


class EventQueue:
    def __init__(self) -> None:
        self._q: "queue.Queue[Event]" = queue.Queue()

    def put(self, event: Event) -> None:
        self._q.put(event)

    def get(self) -> Optional[Event]:
        """Non-blocking pop. Returns None when empty."""
        try:
            return self._q.get(block=False)
        except queue.Empty:
            return None

    def drain(self) -> Iterator[Event]:
        """Yield all currently queued events until empty."""
        while True:
            event = self.get()
            if event is None:
                return
            yield event

    def __len__(self) -> int:
        return self._q.qsize()
