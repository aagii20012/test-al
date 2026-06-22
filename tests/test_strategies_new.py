"""Smoke tests for the new strategy families: they run end-to-end through the
shared engine, produce an equity curve, and stay finite under realistic costs."""

import numpy as np

from algotrading.research.regimes import make_regime_frames
from algotrading.research.runner import run_backtest
from algotrading.risk.risk_manager import RiskConfig
from algotrading.strategy.bollinger import BollingerReversionStrategy
from algotrading.strategy.donchian_breakout import DonchianBreakoutStrategy
from algotrading.strategy.momentum import MomentumStrategy

STRATS = [
    (DonchianBreakoutStrategy, {"entry": 20, "exit": 10, "trend": 0}),
    (MomentumStrategy, {"lookback": 24, "threshold": 0.0}),
    (BollingerReversionStrategy, {"window": 20, "entry_z": 2.0, "exit_z": 0.5}),
]


def test_new_strategies_run_and_stay_finite():
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=800, seed=5)
    for cls, params in STRATS:
        res = run_backtest(frames, cls, params, RiskConfig(),
                           commission_pct=0.001, slippage_bps=2.0)
        eq = res.equity_df
        assert len(eq) > 0
        assert np.isfinite(res.final_equity)
        assert res.final_equity > 0


def test_donchian_trend_filter_reduces_trades():
    """A trend filter should never increase the number of entries."""
    frames = make_regime_frames("chop", ["BTCUSDT"], n_bars=1500, seed=9)
    no_filter = run_backtest(frames, DonchianBreakoutStrategy,
                             {"entry": 20, "exit": 10, "trend": 0}, RiskConfig())
    with_filter = run_backtest(frames, DonchianBreakoutStrategy,
                               {"entry": 20, "exit": 10, "trend": 100}, RiskConfig())
    assert with_filter.report.n_trades <= no_filter.report.n_trades


def test_momentum_takes_short_in_bear_when_allowed():
    frames = make_regime_frames("bear", ["BTCUSDT"], n_bars=1500, seed=4)
    res = run_backtest(frames, MomentumStrategy,
                       {"lookback": 24, "threshold": 0.0, "allow_short": True},
                       RiskConfig(allow_short=True))
    # In a sustained downtrend a momentum system should at least open trades.
    assert res.report.n_trades > 0
