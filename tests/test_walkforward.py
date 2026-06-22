"""Tests for the optimisation + walk-forward research layer."""

from algotrading.research.optimize import expand_grid, grid_search
from algotrading.research.regimes import make_regime_frames
from algotrading.research.walkforward import walk_forward
from algotrading.risk.risk_manager import RiskConfig
from algotrading.strategy.sma_crossover import SMACrossoverStrategy


def test_expand_grid_cartesian_product():
    grid = {"a": [1, 2], "b": [3, 4, 5]}
    combos = expand_grid(grid)
    assert len(combos) == 6
    assert {"a": 1, "b": 3} in combos
    assert expand_grid({}) == [{}]


def test_grid_search_ranks_best_first():
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=1200, seed=2)
    grid = {"fast": [10, 20], "slow": [50, 100]}
    ranked = grid_search(frames, SMACrossoverStrategy, grid, RiskConfig(),
                         objective="sharpe")
    assert ranked, "grid search returned no results"
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)
    # Invalid combos (fast >= slow) are skipped, never crash.
    for r in ranked:
        assert r.params["fast"] < r.params["slow"]


def test_walk_forward_produces_oos_curve():
    frames = make_regime_frames("bull", ["BTCUSDT"], n_bars=4000, seed=8)
    grid = {"fast": [10, 20], "slow": [50, 100]}
    wf = walk_forward(frames, SMACrossoverStrategy, grid, RiskConfig(),
                      train_bars=1500, test_bars=600, objective="sharpe")
    assert wf.n_windows >= 1
    assert not wf.oos_equity_df.empty
    assert wf.oos_report.initial_equity > 0
    # Every window must have chosen a valid parameter set.
    for w in wf.windows:
        assert "fast" in w.best_params and "slow" in w.best_params
