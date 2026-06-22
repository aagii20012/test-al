"""Synthetic market-regime generators for stress-testing across conditions.

A strategy that only works in one kind of market is a curve-fit. To test
robustness we generate deterministic OHLCV series for four archetypal regimes:

  * bull   — steady positive drift, moderate vol (trend-followers thrive)
  * bear   — steady negative drift (long-only dies; shorts/flat survive)
  * chop   — mean-reverting around a flat level (trend dies; reversion thrives)
  * crash  — calm, then a sharp drawdown with a volatility spike (tests stops)

Everything is seeded, so results are reproducible and comparable run-to-run.
These complement the real BTC history rather than replacing it: synthetic data
isolates a single regime, real data mixes them as they actually occur.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd


def _ohlcv_from_close(close: np.ndarray, index: pd.DatetimeIndex, rng, vol: float) -> pd.DataFrame:
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, vol, len(close))) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.uniform(10, 1000, len(close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def make_regime_frames(
    regime: str,
    symbols: List[str],
    n_bars: int = 4000,
    interval_minutes: int = 60,
    start_price: float = 30_000.0,
    seed: int = 7,
) -> Dict[str, pd.DataFrame]:
    """Generate a {symbol -> OHLCV DataFrame} for the named regime."""
    rng = np.random.default_rng(seed)
    start = datetime(2022, 1, 1)
    index = pd.DatetimeIndex(
        [start + timedelta(minutes=interval_minutes * i) for i in range(n_bars)]
    )

    frames: Dict[str, pd.DataFrame] = {}
    for k, symbol in enumerate(symbols):
        sp = start_price * (1 + 0.1 * k)
        if regime == "bull":
            mu, sigma = 0.0004, 0.012
            close = sp * np.exp(np.cumsum(rng.normal(mu, sigma, n_bars)))
            vol = sigma
        elif regime == "bear":
            mu, sigma = -0.0004, 0.014
            close = sp * np.exp(np.cumsum(rng.normal(mu, sigma, n_bars)))
            vol = sigma
        elif regime == "chop":
            # Ornstein-Uhlenbeck mean reversion around log(sp): no net drift.
            theta, sigma = 0.05, 0.012
            x = np.zeros(n_bars)
            target = np.log(sp)
            x[0] = target
            for t in range(1, n_bars):
                x[t] = x[t - 1] + theta * (target - x[t - 1]) + rng.normal(0, sigma)
            close = np.exp(x)
            vol = sigma
        elif regime == "crash":
            sigma = 0.010
            shocks = rng.normal(0.0002, sigma, n_bars)
            crash_at = int(n_bars * 0.6)
            # Sustained negative drift + vol spike for ~5% of the series.
            span = max(1, int(n_bars * 0.05))
            shocks[crash_at:crash_at + span] = rng.normal(-0.02, sigma * 3, span)
            close = sp * np.exp(np.cumsum(shocks))
            vol = sigma
        else:
            raise ValueError(f"unknown regime {regime!r}")

        frames[symbol] = _ohlcv_from_close(close, index, rng, vol)
    return frames


REGIMES = ("bull", "bear", "chop", "crash")
