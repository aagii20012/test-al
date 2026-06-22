"""Tests for the volatility-breakout strategy and the dollar-denominated
performance metrics (net profit, avg daily profit, return on capital)."""

import numpy as np

from algotrading.analytics.performance import compute_report
from algotrading.research.regimes import make_regime_frames
from algotrading.research.runner import run_backtest
from algotrading.risk.risk_manager import RiskConfig
from algotrading.strategy.volatility_breakout import VolatilityBreakoutStrategy


def test_volatility_strategy_runs_and_is_finite():
    frames = make_regime_frames("crash", ["BTCUSDT"], n_bars=1500, seed=6)
    res = run_backtest(frames, VolatilityBreakoutStrategy,
                       {"window": 20, "kc_mult": 1.5, "squeeze_lookback": 6},
                       RiskConfig(), commission_pct=0.001, slippage_bps=2.0)
    assert len(res.equity_df) > 0
    assert np.isfinite(res.final_equity) and res.final_equity > 0


def test_volatility_squeeze_filter_is_selective():
    # Requiring a squeeze should never produce more entries than not requiring one.
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=2000, seed=1)
    gated = run_backtest(frames, VolatilityBreakoutStrategy,
                         {"window": 20, "use_squeeze": True}, RiskConfig())
    ungated = run_backtest(frames, VolatilityBreakoutStrategy,
                           {"window": 20, "use_squeeze": False}, RiskConfig())
    assert gated.report.n_trades <= ungated.report.n_trades


def test_dollar_metrics_consistent_with_equity():
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=1500, seed=3)
    res = run_backtest(frames, VolatilityBreakoutStrategy, {"window": 20},
                       RiskConfig(), initial_capital=100.0)
    r = res.report
    # Net profit and ROC must reconcile with the equity endpoints.
    assert abs(r.net_profit - (r.final_equity - r.initial_equity)) < 1e-6
    assert abs(r.return_on_capital_pct - r.total_return_pct) < 1e-6
    assert r.initial_equity == 100.0


def test_avg_daily_profit_matches_daily_resample():
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=2400, seed=2)
    res = run_backtest(frames, VolatilityBreakoutStrategy, {"window": 20},
                       RiskConfig(), initial_capital=1000.0)
    eq = res.equity_df["equity"]
    expected = float(eq.resample("1D").last().dropna().diff().dropna().mean())
    assert abs(res.report.avg_daily_profit - expected) < 1e-6
