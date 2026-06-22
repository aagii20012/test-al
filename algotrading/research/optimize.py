"""Objective parameter optimisation via grid search.

`grid_search` runs every combination in a parameter grid over a *single* data
slice and ranks them by an objective function. On its own this is in-sample
optimisation — prone to overfitting — so it is only ever used as the inner
"training" step of walk-forward analysis (see walkforward.py), never as a
standalone result.

The default objective is the Sharpe ratio with a soft penalty for parameter sets
that trade too rarely to be statistically meaningful (a classic overfit tell:
a 3-trade backtest with a dazzling Sharpe).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Type

import pandas as pd

from ..analytics.performance import PerformanceReport
from ..risk.risk_manager import RiskConfig
from ..strategy.base import Strategy
from .runner import RunResult, run_backtest


def sharpe_objective(r: PerformanceReport, min_trades: int = 10) -> float:
    """Sharpe, penalised when there are too few trades to trust the estimate."""
    if r.n_trades < min_trades:
        return r.sharpe * (r.n_trades / min_trades)
    return r.sharpe


def calmar_objective(r: PerformanceReport, min_trades: int = 10) -> float:
    score = r.calmar
    if r.n_trades < min_trades:
        score *= r.n_trades / min_trades
    return score


OBJECTIVES: Dict[str, Callable[[PerformanceReport], float]] = {
    "sharpe": sharpe_objective,
    "calmar": calmar_objective,
    "sortino": lambda r: r.sortino,
    "total_return": lambda r: r.total_return_pct,
}


@dataclass
class OptResult:
    params: dict
    score: float
    report: PerformanceReport


def expand_grid(grid: Dict[str, list]) -> List[dict]:
    """Cartesian product of a {param -> [values]} grid into a list of dicts."""
    if not grid:
        return [{}]
    keys = list(grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*grid.values())]


def grid_search(
    frames: Dict[str, pd.DataFrame],
    strategy_cls: Type[Strategy],
    grid: Dict[str, list],
    risk_config: Optional[RiskConfig] = None,
    *,
    objective: str | Callable = "sharpe",
    commission_pct: float = 0.001,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
    periods_per_year: float = 365 * 24,
    exec_realism: Optional[dict] = None,
    financing_apr: float = 0.0,
) -> List[OptResult]:
    """Evaluate every grid combination; return results sorted best-first."""
    obj = OBJECTIVES[objective] if isinstance(objective, str) else objective
    results: List[OptResult] = []
    for params in expand_grid(grid):
        try:
            run: RunResult = run_backtest(
                frames, strategy_cls, params, risk_config,
                initial_capital=initial_capital,
                commission_pct=commission_pct, slippage_bps=slippage_bps,
                periods_per_year=periods_per_year, exec_realism=exec_realism,
                financing_apr=financing_apr,
            )
        except (ValueError, ZeroDivisionError):
            # e.g. fast >= slow window — skip invalid combinations.
            continue
        results.append(OptResult(params=params, score=obj(run.report), report=run.report))
    results.sort(key=lambda r: (r.score if r.score == r.score else -1e9), reverse=True)
    return results
