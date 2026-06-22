"""Single-backtest runner — the one place that wires the whole pipeline.

Both the CLI and every research routine call `run_backtest`, so there is exactly
one definition of "run this strategy with this risk policy over these frames".
That guarantees the optimisation/walk-forward results use the identical engine
that backtest and live trading use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Type

import pandas as pd

from ..analytics.performance import PerformanceReport, compute_report
from ..core.event_queue import EventQueue
from ..data.historical import HistoricCSVDataHandler
from ..engine.backtest import BacktestEngine
from ..execution.simulated import SimulatedExecutionHandler
from ..portfolio.portfolio import Portfolio
from ..risk.risk_manager import RiskConfig, RiskManager
from ..strategy.base import Strategy


@dataclass
class RunResult:
    report: PerformanceReport
    equity_df: pd.DataFrame
    trade_log: list
    final_equity: float
    initial_capital: float
    total_commission: float = 0.0
    total_slippage: float = 0.0
    total_financing: float = 0.0


def run_backtest(
    frames: Dict[str, pd.DataFrame],
    strategy_cls: Type[Strategy],
    params: Optional[dict] = None,
    risk_config: Optional[RiskConfig] = None,
    *,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.001,
    slippage_bps: float = 2.0,
    periods_per_year: float = 365 * 24,
    exec_realism: Optional[dict] = None,
    financing_apr: float = 0.0,
) -> RunResult:
    """Run one backtest end-to-end and return its result bundle.

    Every call builds *fresh* stateful objects (queue, data cursor, portfolio,
    risk manager) so runs are fully independent and repeatable.

    `exec_realism` is an optional dict of SimulatedExecutionHandler kwargs
    (e.g. {"fill_at": "next_open", "participation_rate": 0.05, "min_notional": 10,
    "impact_coeff_bps": 50}) to model latency, partial fills, exchange dust
    limits, and market impact. `financing_apr` charges per-bar borrow/funding on
    short and leveraged exposure.
    """
    params = params or {}
    risk_config = risk_config or RiskConfig()

    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(risk_config)
    portfolio = Portfolio(data, events, risk, initial_capital=initial_capital,
                          financing_apr=financing_apr, periods_per_year=periods_per_year)
    execution = SimulatedExecutionHandler(
        events, data, commission_pct=commission_pct, slippage_bps=slippage_bps,
        **(exec_realism or {}),
    )
    strategy = strategy_cls(data, events, **params)

    BacktestEngine(data, strategy, portfolio, execution, events).run()

    equity_df = portfolio.equity_dataframe()
    report = compute_report(
        equity_df,
        portfolio.trade_log,
        portfolio.total_commission,
        periods_per_year=periods_per_year,
        total_slippage=portfolio.total_slippage,
        total_financing=portfolio.total_financing,
    )
    return RunResult(
        report=report,
        equity_df=equity_df,
        trade_log=portfolio.trade_log,
        final_equity=portfolio.equity,
        initial_capital=initial_capital,
        total_commission=portfolio.total_commission,
        total_slippage=portfolio.total_slippage,
        total_financing=portfolio.total_financing,
    )


def slice_frames(frames: Dict[str, pd.DataFrame], start: int, end: int) -> Dict[str, pd.DataFrame]:
    """Positional [start:end) slice of every symbol's frame (walk-forward windows)."""
    return {s: df.iloc[start:end] for s, df in frames.items()}


def n_bars(frames: Dict[str, pd.DataFrame]) -> int:
    """Length of the longest symbol frame (the union timeline length)."""
    return max((len(df) for df in frames.values()), default=0)
