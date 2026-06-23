"""Free public OHLCV (Coinbase Exchange) for cloud paper-simulation.

Binance geo-blocks cloud-datacenter IPs (e.g. GitHub Actions runners), so the
simulated cloud bot reads REAL prices from Coinbase's public, key-less candles
endpoint instead. It exposes the same `fetch_ohlcv(symbol, interval, limit)`
signature as the exchange adapters, so it drops straight into LiveDataHandler.

This is market data only — there is no account and no order placement; fills are
simulated locally by SimulatedExecutionHandler. No API key, no geo-block.
"""

from __future__ import annotations

import time

import pandas as pd

from ..utils.logger import get_logger

log = get_logger(__name__)

# Coinbase supports only these candle granularities (seconds).
_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
_BASE = "https://api.exchange.coinbase.com"


class PublicMarketData:
    """Drop-in market-data source backed by Coinbase's public candles API."""

    name = "coinbase-public"

    @staticmethod
    def _product(symbol: str) -> str:
        # Map a Binance-style symbol to a Coinbase product id: BTCUSDT -> BTC-USD.
        if symbol.endswith("USDT"):
            return symbol[:-4] + "-USD"
        if symbol.endswith("USD"):
            return symbol[:-3] + "-USD"
        return symbol

    def fetch_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 300,
                    days=None) -> pd.DataFrame:
        import requests

        gran = _GRANULARITY.get(interval)
        if gran is None:
            raise ValueError(
                f"interval {interval!r} not supported by the public source; "
                f"use one of {sorted(_GRANULARITY)}")
        product = self._product(symbol)
        resp = requests.get(
            f"{_BASE}/products/{product}/candles",
            params={"granularity": gran},
            headers={"User-Agent": "algotrading/1.0"},  # Coinbase 403s a blank UA
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        # Coinbase rows: [time, low, high, open, close, volume], newest first.
        df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("dt").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)

        # Keep only CLOSED candles, decided by the clock — not by position.
        # Coinbase may not have created the current bucket yet (low volume), so
        # "the last row is the forming one" is unreliable and can drop a bar that
        # actually just closed. A candle that opened at t is closed once
        # t + interval <= now.
        open_epoch = df.index.astype("int64") // 1_000_000_000
        df = df[open_epoch + gran <= time.time()]
        return df.tail(limit)
