"""Live data handler that polls closed Binance klines.

Design note: we deliberately act only on *closed* bars (not partial/ticking
candles). This keeps live behaviour identical to the bar-based backtester — a
strategy that reads `bar.close` gets a final value, never a flickering one.

Polling (rather than websockets) is used for clarity and dependency-lightness;
the interface is the same, so a websocket implementation can replace this class
without affecting strategies.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, List

from ..core.event_queue import EventQueue
from ..core.events import MarketEvent
from ..utils.logger import get_logger
from .base import Bar, DataHandler

log = get_logger(__name__)

# Approx milliseconds per interval, used to time polling.
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


class LiveDataHandler(DataHandler):
    def __init__(
        self,
        events: EventQueue,
        exchange,
        symbols: List[str],
        interval: str = "1m",
        history: int = 500,
        poll_seconds: float = 2.0,
        drop_forming: bool = False,
    ):
        self.events = events
        self.exchange = exchange
        self.symbols = symbols
        self.interval = interval
        self.poll_seconds = poll_seconds
        self._continue = True
        self._buffers: Dict[str, Deque[Bar]] = defaultdict(lambda: deque(maxlen=history))
        self._last_open_ts: Dict[str, int] = {}

        # Warm up with recent history so indicators have lookback immediately.
        # `drop_forming` excludes the final (still-forming) candle so the buffer
        # ends exactly on the last CLOSED bar — used by the run-once "tick" mode.
        for symbol in symbols:
            df = exchange.fetch_ohlcv(symbol, interval=interval, limit=history)
            if drop_forming and len(df) > 1:
                df = df.iloc[:-1]
            for ts, row in df.iterrows():
                bar = Bar(symbol, ts.to_pydatetime(), float(row["open"]),
                          float(row["high"]), float(row["low"]),
                          float(row["close"]), float(row["volume"]))
                self._buffers[symbol].append(bar)
                self._last_open_ts[symbol] = int(ts.timestamp() * 1000)
            log.info("Warmed up %s with %d bars", symbol, len(self._buffers[symbol]))

    def get_latest_bars(self, symbol: str, n: int = 1) -> List[Bar]:
        return list(self._buffers[symbol])[-n:]

    def get_latest_bar(self, symbol: str) -> Bar | None:
        buf = self._buffers[symbol]
        return buf[-1] if buf else None

    def update_bars(self) -> None:
        """Block until at least one new closed bar arrives, then emit it."""
        got_new = False
        last_beat = time.monotonic()
        while not got_new and self._continue:
            for symbol in self.symbols:
                df = self.exchange.fetch_ohlcv(symbol, interval=self.interval, limit=2)
                if df.empty:
                    continue
                # The last row may be the still-forming candle; take the prior closed one.
                closed = df.iloc[:-1]
                if closed.empty:
                    continue
                ts = closed.index[-1]
                open_ms = int(ts.timestamp() * 1000)
                if open_ms > self._last_open_ts.get(symbol, 0):
                    row = closed.iloc[-1]
                    bar = Bar(symbol, ts.to_pydatetime(), float(row["open"]),
                              float(row["high"]), float(row["low"]),
                              float(row["close"]), float(row["volume"]))
                    self._buffers[symbol].append(bar)
                    self._last_open_ts[symbol] = open_ms
                    got_new = True
                    log.info("New %s %s bar closed @ %s | close=%.2f",
                             symbol, self.interval, ts, bar.close)
            if not got_new:
                # Heartbeat every ~30s so the operator can see it's alive and
                # waiting for the next bar to close (a 5m/1h bar is mostly silence).
                now = time.monotonic()
                if now - last_beat >= 30:
                    px = self.get_latest_bar(self.symbols[0])
                    log.info("Waiting for next %s bar to close... (last close %.2f)",
                             self.interval, px.close if px else float("nan"))
                    last_beat = now
                time.sleep(self.poll_seconds)

        if got_new:
            # Use a datetime (matching the backtest's MarketEvent.dt type) so the
            # equity-curve index is consistent across backtest and live.
            self.events.put(MarketEvent(dt=datetime.now(timezone.utc)))

    def stop(self) -> None:
        self._continue = False

    @property
    def continue_trading(self) -> bool:
        return self._continue
