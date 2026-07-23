"""Immutable market snapshot: one shared boundary, one hash, strict validation.

All offline. Malformed candle streams (empty, missing column, off-grid,
duplicate, unordered), an unreachable shared boundary, and post-hoc tampering
must all be rejected loudly rather than silently trimmed or resumed.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import ANCHOR, DuplicateFetch, GrowingFetch, scripted_frames  # noqa: E402

from algotrading.gen2.snapshot import (  # noqa: E402
    MarketSnapshot, SnapshotError, SnapshotExchange, build_snapshot)

PRODUCTS = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD"}


def _build(fetch, products=None, **kw):
    return build_snapshot("gen2-test", products or {"BTCUSDT": "BTC-USD"},
                          fetch_ohlcv=fetch, interval="1h", history=300, **kw)


# --------------------------------------------------------------------------
# shared boundary
# --------------------------------------------------------------------------
def test_shared_boundary_is_latest_common_closed_hour():
    btc = scripted_frames(("BTCUSDT",), phases=[(120, 0.01), (30, -0.02)])   # 150
    eth = scripted_frames(("ETHUSDT",), phases=[(120, 0.01), (25, -0.02)])   # 145
    snap = _build(GrowingFetch({**btc, **eth}), PRODUCTS)

    expected = int((ANCHOR + timedelta(hours=144)).timestamp() * 1000)
    assert snap.shared_candle_epoch_ms == expected
    assert snap.candles["BTCUSDT"][-1][0] == expected
    assert snap.candles["ETHUSDT"][-1][0] == expected
    assert sorted(snap.symbols) == ["BTCUSDT", "ETHUSDT"]


def test_no_common_boundary_rejected():
    btc = scripted_frames(("BTCUSDT",), start=ANCHOR)
    eth = scripted_frames(("ETHUSDT",), start=ANCHOR + timedelta(hours=5000))
    with pytest.raises(SnapshotError) as ei:
        _build(GrowingFetch({**btc, **eth}), PRODUCTS)
    assert ei.value.kind == "no_common_boundary"


# --------------------------------------------------------------------------
# one hash, reproducible, hash-verified
# --------------------------------------------------------------------------
def test_snapshot_hash_is_deterministic_and_verifies():
    frames = scripted_frames(("BTCUSDT",))
    a = _build(GrowingFetch(frames))
    b = _build(GrowingFetch(frames))
    assert a.sha256 == b.sha256
    assert a.sha256 == a.recompute_hash()
    a.verify()   # no raise

    # round-trip through dict preserves and re-verifies the hash
    rt = MarketSnapshot.from_dict(a.to_dict())
    assert rt.sha256 == a.sha256


def test_frame_rebuilt_from_hashed_bytes_matches_candles():
    frames = scripted_frames(("BTCUSDT",))
    snap = _build(GrowingFetch(frames))
    frame = snap.frame("BTCUSDT")
    rows = snap.candles["BTCUSDT"]
    assert len(frame) == len(rows)
    # last close in the rebuilt frame equals the last candle's close field
    assert float(frame["close"].iloc[-1]) == rows[-1][4]
    # index is UTC and lands on the hourly grid
    assert str(frame.index.tz) == "UTC"
    assert int(frame.index[-1].timestamp()) * 1000 == rows[-1][0]


def test_tampered_snapshot_detected():
    snap = _build(GrowingFetch(scripted_frames(("BTCUSDT",))))
    snap.candles["BTCUSDT"][-1][4] = 999999.0    # rewrite the last close
    assert snap.recompute_hash() != snap.sha256
    with pytest.raises(SnapshotError) as ei:
        snap.verify()
    assert ei.value.kind == "corrupted"


# --------------------------------------------------------------------------
# malformed candle streams
# --------------------------------------------------------------------------
def test_empty_frame_rejected():
    class EmptyFetch:
        def __call__(self, symbol, interval="1h", limit=300):
            return pd.DataFrame(
                {c: [] for c in ["open", "high", "low", "close", "volume"]},
                index=pd.DatetimeIndex([]))

    with pytest.raises(SnapshotError) as ei:
        _build(EmptyFetch())
    assert ei.value.kind == "empty"


def test_missing_column_rejected():
    frames = scripted_frames(("BTCUSDT",))

    class NoCloseFetch:
        def __call__(self, symbol, interval="1h", limit=300):
            return frames[symbol].drop(columns=["close"]).tail(limit)

    with pytest.raises(SnapshotError) as ei:
        _build(NoCloseFetch())
    assert ei.value.kind == "malformed"


def test_non_hourly_grid_rejected():
    class OffGridFetch:
        def __call__(self, symbol, interval="1h", limit=300):
            n = 150
            idx = pd.DatetimeIndex(
                [ANCHOR + timedelta(hours=k, minutes=30) for k in range(n)])
            return pd.DataFrame(
                {"open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
                 "close": [1.0] * n, "volume": [1.0] * n}, index=idx)

    with pytest.raises(SnapshotError) as ei:
        _build(OffGridFetch())
    assert ei.value.kind == "non_hourly"


def test_duplicate_candle_rejected():
    with pytest.raises(SnapshotError) as ei:
        _build(DuplicateFetch(scripted_frames(("BTCUSDT",))))
    assert ei.value.kind == "duplicate"


def test_unordered_candles_rejected():
    frames = scripted_frames(("BTCUSDT",))

    class ReversedFetch:
        def __call__(self, symbol, interval="1h", limit=300):
            return frames[symbol].tail(limit).iloc[::-1]

    with pytest.raises(SnapshotError) as ei:
        _build(ReversedFetch())
    assert ei.value.kind == "unordered"


def test_insufficient_warmup_rejected():
    # Only 40 candles up to the boundary; below MIN_WARMUP=110.
    frames = scripted_frames(("BTCUSDT",), phases=[(40, 0.01)])
    with pytest.raises(SnapshotError) as ei:
        _build(GrowingFetch(frames))
    assert ei.value.kind == "insufficient_warmup"


# --------------------------------------------------------------------------
# the snapshot exchange is READ-ONLY market data (no order surface)
# --------------------------------------------------------------------------
def test_snapshot_exchange_has_no_order_surface():
    snap = _build(GrowingFetch(scripted_frames(("BTCUSDT",))))
    ex = SnapshotExchange(snap)
    df = ex.fetch_ohlcv("BTCUSDT", interval="1h", limit=50)
    assert len(df) == 50
    for forbidden in ("place_market_order", "place_order", "create_order",
                      "account_balances", "cancel_order"):
        assert not hasattr(ex, forbidden)
