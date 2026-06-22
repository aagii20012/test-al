"""Historical data handlers for backtesting.

`HistoricCSVDataHandler` streams pre-loaded OHLCV frames bar-by-bar. It can be
fed from:
  * CSV files in a cache directory,
  * the Binance REST API (downloaded once, then cached),
  * a deterministic synthetic generator (no network / no keys needed).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd

from ..core.event_queue import EventQueue
from ..core.events import MarketEvent
from ..utils.logger import get_logger
from .base import Bar, DataHandler

log = get_logger(__name__)

_REQUIRED_COLS = ["open", "high", "low", "close", "volume"]


class HistoricCSVDataHandler(DataHandler):
    """Replays a dict of {symbol -> DataFrame(indexed by datetime)}."""

    def __init__(self, events: EventQueue, frames: Dict[str, pd.DataFrame]):
        self.events = events
        self.symbols = list(frames.keys())
        self._frames = {s: self._validate(df) for s, df in frames.items()}

        # Build a single, union timeline so multi-symbol backtests stay aligned.
        index = sorted(set().union(*[df.index for df in self._frames.values()]))
        self._timeline: List[pd.Timestamp] = index
        self._cursor = -1
        self._latest: Dict[str, List[Bar]] = {s: [] for s in self.symbols}
        self._continue = True

    @staticmethod
    def _validate(df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("DataFrame must be indexed by datetime")
        return df.sort_index()

    # ---- DataHandler interface -------------------------------------------
    def get_latest_bars(self, symbol: str, n: int = 1) -> List[Bar]:
        return self._latest.get(symbol, [])[-n:]

    def get_latest_bar(self, symbol: str) -> Bar | None:
        bars = self._latest.get(symbol, [])
        return bars[-1] if bars else None

    def update_bars(self) -> None:
        self._cursor += 1
        if self._cursor >= len(self._timeline):
            self._continue = False
            return

        ts = self._timeline[self._cursor]
        for symbol, df in self._frames.items():
            if ts in df.index:
                row = df.loc[ts]
                bar = Bar(
                    symbol=symbol,
                    dt=ts.to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                self._latest[symbol].append(bar)
        self.events.put(MarketEvent(dt=ts.to_pydatetime()))

    @property
    def continue_trading(self) -> bool:
        return self._continue


# --------------------------------------------------------------------------
# Frame builders
# --------------------------------------------------------------------------
def load_csv_frames(symbols: List[str], cache_dir: str, interval: str) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = os.path.join(cache_dir, f"{symbol}_{interval}.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No cached data at {path}. Run `download` first or use --synthetic."
            )
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        frames[symbol] = df
        log.info("Loaded %d bars for %s from cache", len(df), symbol)
    return frames


def make_synthetic_frames(
    symbols: List[str],
    n_bars: int = 2000,
    interval_minutes: int = 60,
    start_price: float = 30_000.0,
    seed: int = 42,
) -> Dict[str, pd.DataFrame]:
    """Geometric-brownian-motion OHLCV — deterministic given `seed`.

    Lets the whole pipeline (and tests) run with zero external dependencies.
    """
    rng = np.random.default_rng(seed)
    start = datetime(2023, 1, 1)
    index = pd.DatetimeIndex(
        [start + timedelta(minutes=interval_minutes * i) for i in range(n_bars)]
    )

    frames: Dict[str, pd.DataFrame] = {}
    for k, symbol in enumerate(symbols):
        mu, sigma = 0.00002, 0.01
        shocks = rng.normal(mu, sigma, n_bars)
        close = start_price * (1 + k * 0.1) * np.exp(np.cumsum(shocks))
        open_ = np.concatenate([[close[0]], close[:-1]])
        spread = np.abs(rng.normal(0, sigma, n_bars)) * close
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volume = rng.uniform(10, 1000, n_bars)
        frames[symbol] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )
        log.info("Generated %d synthetic bars for %s", n_bars, symbol)
    return frames


def download_binance_frames(
    symbols: List[str],
    interval: str,
    days: int,
    cache_dir: str,
    exchange=None,
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV from Binance REST and cache as CSV."""
    from ..exchange.binance import BinanceExchange

    os.makedirs(cache_dir, exist_ok=True)
    # Public market data must come from mainnet — the testnet only retains a
    # small window of recent klines. No API keys are needed for public data.
    exchange = exchange or BinanceExchange(testnet=False)
    frames: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = exchange.fetch_ohlcv(symbol, interval=interval, days=days)
        path = os.path.join(cache_dir, f"{symbol}_{interval}.csv")
        df.to_csv(path)
        frames[symbol] = df
        log.info("Downloaded & cached %d bars for %s -> %s", len(df), symbol, path)
    return frames
