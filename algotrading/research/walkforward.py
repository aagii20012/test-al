"""Walk-forward analysis — the project's defence against overfitting.

In-sample optimisation always finds *something* that looks great on the data it
was fitted to. The only honest question is: do parameters chosen on the past
hold up on data they have never seen? Walk-forward answers it directly:

    ├─ train ─┤├ test ┤
              ├─ train ─┤├ test ┤
                        ├─ train ─┤├ test ┤   ...

For each step we optimise parameters on the *train* window, then trade them
unchanged on the immediately following *test* window — capital carried forward,
so the stitched test segments form one continuous out-of-sample equity curve.
Every dollar in that curve was earned on data the optimiser never saw, which is
exactly the situation live trading faces. The aggregate metrics on that curve
are the only ones worth quoting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type

import pandas as pd

from ..analytics.performance import PerformanceReport, compute_report
from ..risk.risk_manager import RiskConfig
from ..strategy.base import Strategy
from .optimize import grid_search
from .runner import n_bars, run_backtest, slice_frames


@dataclass
class Window:
    index: int
    train_range: tuple
    test_range: tuple
    best_params: dict
    train_score: float
    test_report: PerformanceReport


@dataclass
class WalkForwardResult:
    strategy: str
    windows: List[Window]
    oos_equity_df: pd.DataFrame
    oos_report: PerformanceReport
    oos_trade_log: list = field(default_factory=list)

    @property
    def n_windows(self) -> int:
        return len(self.windows)


def walk_forward(
    frames: Dict[str, pd.DataFrame],
    strategy_cls: Type[Strategy],
    grid: Dict[str, list],
    risk_config: Optional[RiskConfig] = None,
    *,
    train_bars: int,
    test_bars: int,
    objective: str = "sharpe",
    commission_pct: float = 0.001,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
    periods_per_year: float = 365 * 24,
    exec_realism: Optional[dict] = None,
    financing_apr: float = 0.0,
) -> WalkForwardResult:
    total = n_bars(frames)
    if total < train_bars + test_bars:
        raise ValueError(
            f"need >= train+test ({train_bars + test_bars}) bars, have {total}"
        )

    windows: List[Window] = []
    equity_parts: List[pd.DataFrame] = []
    trade_log: list = []
    total_commission = 0.0
    total_slippage = 0.0
    total_financing = 0.0

    running_equity = initial_capital
    start = 0
    widx = 0
    while start + train_bars + test_bars <= total:
        tr0, tr1 = start, start + train_bars
        te0, te1 = tr1, tr1 + test_bars

        train_frames = slice_frames(frames, tr0, tr1)
        test_frames = slice_frames(frames, te0, te1)

        # --- in-sample: choose parameters on the training window -----------
        ranked = grid_search(
            train_frames, strategy_cls, grid, risk_config,
            objective=objective, commission_pct=commission_pct,
            slippage_bps=slippage_bps, initial_capital=initial_capital,
            periods_per_year=periods_per_year, exec_realism=exec_realism,
            financing_apr=financing_apr,
        )
        best = ranked[0] if ranked else None
        best_params = best.params if best else {}
        train_score = best.score if best else float("nan")

        # --- out-of-sample: trade those parameters on the next window ------
        oos = run_backtest(
            test_frames, strategy_cls, best_params, risk_config,
            initial_capital=running_equity, commission_pct=commission_pct,
            slippage_bps=slippage_bps, periods_per_year=periods_per_year,
            exec_realism=exec_realism, financing_apr=financing_apr,
        )

        windows.append(Window(
            index=widx, train_range=(tr0, tr1), test_range=(te0, te1),
            best_params=best_params, train_score=train_score, test_report=oos.report,
        ))
        if not oos.equity_df.empty:
            equity_parts.append(oos.equity_df[["equity"]])
        trade_log.extend(oos.trade_log)
        total_commission += oos.total_commission
        total_slippage += oos.total_slippage
        total_financing += oos.total_financing
        running_equity = oos.final_equity

        start += test_bars
        widx += 1

    # --- stitch the continuous out-of-sample equity curve ------------------
    if equity_parts:
        oos_equity = pd.concat(equity_parts)
        oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
        oos_equity["returns"] = oos_equity["equity"].pct_change().fillna(0.0)
    else:
        oos_equity = pd.DataFrame(columns=["equity", "returns"])

    oos_report = compute_report(
        oos_equity, trade_log, total_commission, periods_per_year=periods_per_year,
        total_slippage=total_slippage, total_financing=total_financing,
    )
    return WalkForwardResult(
        strategy=strategy_cls.__name__,
        windows=windows,
        oos_equity_df=oos_equity,
        oos_report=oos_report,
        oos_trade_log=trade_log,
    )
