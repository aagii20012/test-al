"""Immutable, hash-verified market snapshot shared by all 8 bots in a tick.

The coordinator fetches closed candles for every product ONCE, validates them
(strictly increasing, unique, on the hourly grid), selects a single shared
decision boundary (the latest closed hour present in EVERY product), freezes the
exact warm-up windows into a canonical JSON blob, and hashes it (SHA-256). Every
one of the 8 bots is then warmed up from frames reconstructed *from that hashed
blob* — so all bots provably read identical bytes, and the tick is reproducible
and auditable after the fact.

``SnapshotExchange`` presents the frozen per-symbol frames through the same
``fetch_ohlcv(symbol, interval, limit)`` interface the production
``LiveDataHandler`` already consumes, so a coordinator tick reuses the exact,
already-tested warm-up + decision path of ``cmd_tick --simulated`` with no new
market-data code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import pandas as pd

from .experiment import HISTORY, INTERVAL, canonical_json, sha256_bytes

# Milliseconds per supported interval (only 1h is used, but keep it general).
_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                     "6h": 21600, "1d": 86400}

# Enough warm-up for every strategy in the roster (donchian trend=100 is the
# hungriest; momentum lookback=96 needs 97). Below this we refuse to decide
# rather than emit an under-warmed (silent no-signal) tick.
MIN_WARMUP = 110

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


class SnapshotError(RuntimeError):
    """A snapshot could not be built (bad candles or no shared boundary)."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


@dataclass
class MarketSnapshot:
    experiment_id: str
    interval: str
    products: Dict[str, str]
    shared_candle_epoch_ms: int          # open time of the shared decision candle
    symbols: List[str]
    candles: Dict[str, List[list]]       # symbol -> oldest-first [ts_ms,o,h,l,c,v]
    missing: Dict[str, int]              # symbol -> interior gaps recorded (not fatal)
    sha256: str

    # ---- canonical blob + hashing -----------------------------------------
    def payload(self) -> dict:
        """Exactly the bytes that ``sha256`` covers (excludes the hash itself)."""
        return {
            "experiment_id": self.experiment_id,
            "interval": self.interval,
            "products": self.products,
            "shared_candle_epoch_ms": self.shared_candle_epoch_ms,
            "symbols": self.symbols,
            "candles": self.candles,
            "missing": self.missing,
        }

    def to_dict(self) -> dict:
        d = self.payload()
        d["sha256"] = self.sha256
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MarketSnapshot":
        snap = cls(
            experiment_id=d["experiment_id"],
            interval=d["interval"],
            products=d["products"],
            shared_candle_epoch_ms=d["shared_candle_epoch_ms"],
            symbols=d["symbols"],
            candles=d["candles"],
            missing=d["missing"],
            sha256=d["sha256"],
        )
        snap.verify()
        return snap

    def recompute_hash(self) -> str:
        return sha256_bytes(canonical_json(self.payload()).encode("utf-8"))

    def verify(self) -> None:
        got = self.recompute_hash()
        if got != self.sha256:
            raise SnapshotError(
                "corrupted",
                f"snapshot hash mismatch: stored {self.sha256!r} != recomputed "
                f"{got!r}. The immutable market snapshot has been altered.")

    def frame(self, symbol: str) -> pd.DataFrame:
        """Reconstruct the per-symbol warm-up frame FROM the hashed bytes.

        Matches the shape ``PublicMarketData.fetch_ohlcv`` returns: a UTC
        DatetimeIndex with open/high/low/close/volume float columns, oldest
        first. Because it is rebuilt from the canonical JSON that was hashed,
        every bot demonstrably consumes identical data.
        """
        rows = self.candles[symbol]
        idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
        data = {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }
        return pd.DataFrame(data, index=idx)


class SnapshotExchange:
    """Serves frozen snapshot frames via the LiveDataHandler exchange interface.

    ``LiveDataHandler`` calls ``fetch_ohlcv(symbol, interval, limit)`` once per
    symbol at warm-up; we return the frozen frame's tail. ``update_bars`` is
    never called by the coordinator (a single MarketEvent is injected instead),
    so no polling / network path is ever exercised here.
    """

    def __init__(self, snapshot: MarketSnapshot):
        self._snapshot = snapshot

    def fetch_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 300):
        df = self._snapshot.frame(symbol)
        return df.tail(limit)


# --------------------------------------------------------------------------
# Building a snapshot from a fetch callable (injectable for offline tests).
# --------------------------------------------------------------------------
def _validate_and_epochs(symbol: str, df: pd.DataFrame, interval: str) -> List[int]:
    """Return sorted epoch-seconds for a validated frame or raise SnapshotError."""
    if df is None or len(df) == 0:
        raise SnapshotError("empty", f"{symbol}: no candles returned")
    for col in CANDLE_COLUMNS:
        if col not in df.columns:
            raise SnapshotError(
                "malformed", f"{symbol}: missing required column {col!r}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise SnapshotError("malformed", f"{symbol}: index is not datetime")

    epochs = [int(ts.timestamp()) for ts in df.index]
    gran = _INTERVAL_SECONDS[interval]

    prev = None
    for e in epochs:
        if e % gran != 0:
            raise SnapshotError(
                "non_hourly",
                f"{symbol}: candle open {e} is not aligned to the {interval} grid")
        if prev is not None:
            if e == prev:
                raise SnapshotError(
                    "duplicate", f"{symbol}: duplicate candle at epoch {e}")
            if e < prev:
                raise SnapshotError(
                    "unordered",
                    f"{symbol}: candle {e} is older than its predecessor {prev}")
        prev = e
    return epochs


def build_snapshot(
    experiment_id: str,
    products: Dict[str, str],
    *,
    fetch_ohlcv: Callable[..., pd.DataFrame],
    interval: str = INTERVAL,
    history: int = HISTORY,
    min_warmup: int = MIN_WARMUP,
) -> MarketSnapshot:
    """Fetch ONCE, validate, pick the shared boundary, freeze + hash.

    ``fetch_ohlcv(symbol, interval, limit)`` must return a UTC-indexed OHLCV
    DataFrame of CLOSED candles (``PublicMarketData.fetch_ohlcv`` satisfies this;
    tests inject a deterministic synthetic fetch).
    """
    symbols = sorted(products.keys())
    frames: Dict[str, pd.DataFrame] = {}
    epoch_sets: Dict[str, set] = {}

    for symbol in symbols:
        df = fetch_ohlcv(symbol, interval=interval, limit=history)
        epochs = _validate_and_epochs(symbol, df, interval)
        # index frame by epoch-seconds for boundary math + slicing
        df = df.copy()
        df.index = pd.Index(epochs, name="epoch")
        frames[symbol] = df
        epoch_sets[symbol] = set(epochs)

    # Shared decision boundary = latest hour present in EVERY product.
    common = set.intersection(*epoch_sets.values()) if epoch_sets else set()
    if not common:
        raise SnapshotError(
            "no_common_boundary",
            "no candle timestamp is present in every product; cannot pick a "
            "shared decision boundary")
    decision_epoch = max(common)

    gran = _INTERVAL_SECONDS[interval]
    candles: Dict[str, List[list]] = {}
    missing: Dict[str, int] = {}
    for symbol in symbols:
        df = frames[symbol]
        window = df[df.index <= decision_epoch].tail(history)
        if len(window) < min_warmup:
            raise SnapshotError(
                "insufficient_warmup",
                f"{symbol}: only {len(window)} candles up to the shared boundary; "
                f"need >= {min_warmup} to warm up every strategy")
        win_epochs = list(window.index)
        # Record interior gaps (informational; not fatal — mirrors production
        # tolerance, but the coordinator can surface it in the audit record).
        span = (win_epochs[-1] - win_epochs[0]) // gran + 1
        missing[symbol] = int(span - len(win_epochs))
        rows = []
        for e, (_, r) in zip(win_epochs, window.iterrows()):
            rows.append([
                int(e * 1000),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]),
            ])
        candles[symbol] = rows

    snap = MarketSnapshot(
        experiment_id=experiment_id,
        interval=interval,
        products=dict(products),
        shared_candle_epoch_ms=int(decision_epoch * 1000),
        symbols=symbols,
        candles=candles,
        missing=missing,
        sha256="",
    )
    snap.sha256 = snap.recompute_hash()
    return snap


def load_snapshot(path: str) -> MarketSnapshot:
    with open(path, "r", encoding="utf-8") as fh:
        return MarketSnapshot.from_dict(json.load(fh))
