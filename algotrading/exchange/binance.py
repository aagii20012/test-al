"""Binance exchange adapter (spot).

Public market data (fetch_ohlcv) works without API keys, so backtests on real
history need no credentials. Trading requires keys and, strongly recommended,
the testnet (`testnet=True`).

`python-binance` is imported lazily so the package installs/imports cleanly in
offline/synthetic-only environments.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from ..core.enums import Direction
from ..core.events import FillEvent
from ..utils.logger import get_logger
from .base import Exchange

log = get_logger(__name__)


class BinanceExchange(Exchange):
    name = "binance"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ):
        try:
            from binance.client import Client
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "python-binance is required for live/REST. `pip install python-binance`"
            ) from exc

        self.client = Client(api_key, api_secret, testnet=testnet)
        self._filters: Dict[str, dict] = {}

    # ---- market data -----------------------------------------------------
    def fetch_ohlcv(
        self, symbol: str, interval: str = "1h", limit: int = 500, days: Optional[int] = None
    ) -> pd.DataFrame:
        if days is not None:
            start = f"{days} days ago UTC"
            klines = self.client.get_historical_klines(symbol, interval, start)
        else:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)

        if not klines:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            klines,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "tbav", "tqav", "ignore",
            ],
        )
        df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("dt")[["open", "high", "low", "close", "volume"]].astype(float)
        return df

    # ---- trading ---------------------------------------------------------
    def _symbol_filters(self, symbol: str) -> dict:
        if symbol not in self._filters:
            info = self.client.get_symbol_info(symbol)
            filters = {f["filterType"]: f for f in info["filters"]}
            self._filters[symbol] = filters
        return self._filters[symbol]

    def _round_qty(self, symbol: str, quantity: float) -> float:
        """Round down to the symbol's LOT_SIZE stepSize (exchange rejects otherwise)."""
        step = float(self._symbol_filters(symbol)["LOT_SIZE"]["stepSize"])
        if step == 0:
            return quantity
        precision = int(round(-math.log10(step))) if step < 1 else 0
        return math.floor(quantity / step) * step if precision == 0 else round(
            math.floor(quantity / step) * step, precision
        )

    def place_market_order(
        self, symbol: str, direction: Direction, quantity: float
    ) -> Optional[FillEvent]:
        qty = self._round_qty(symbol, quantity)
        if qty <= 0:
            log.warning("Quantity %.8f rounds to zero for %s; skipping", quantity, symbol)
            return None

        side = "BUY" if direction is Direction.BUY else "SELL"
        resp = self.client.create_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty
        )

        # Aggregate the fills the exchange returns to get an avg price & commission.
        fills = resp.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
            commission = sum(float(f["commission"]) for f in fills)
        else:
            total_qty = float(resp.get("executedQty", qty))
            avg_price = float(resp.get("price", 0)) or self._last_price(symbol)
            commission = 0.0

        return FillEvent(
            dt=datetime.now(timezone.utc),
            symbol=symbol,
            direction=direction,
            quantity=total_qty,
            fill_price=avg_price,
            commission=commission,
            exchange=self.name,
        )

    def _last_price(self, symbol: str) -> float:
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def account_balances(self) -> Dict[str, float]:
        acct = self.client.get_account()
        return {
            b["asset"]: float(b["free"])
            for b in acct["balances"]
            if float(b["free"]) > 0
        }
