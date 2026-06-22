"""Performance analytics computed from the portfolio's equity curve.

All ratios are annualized using `periods_per_year`, which depends on the bar
interval (e.g. hourly bars ~ 24*365). The CLI passes the right value.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict

import numpy as np
import pandas as pd


@dataclass
class PerformanceReport:
    initial_equity: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    annual_volatility_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    calmar: float
    var_95_pct: float
    n_trades: int
    win_rate_pct: float
    total_commission: float
    # ---- extended trade & daily statistics (appended for back-compat) ----
    avg_daily_return_pct: float = 0.0
    profit_factor: float = 0.0
    risk_reward_ratio: float = 0.0   # avg win / avg loss (absolute)
    avg_win: float = 0.0
    avg_loss: float = 0.0
    # ---- dollar-denominated results (for fixed-capital reporting) --------
    net_profit: float = 0.0          # final - initial equity ($), NET of all costs
    avg_daily_profit: float = 0.0    # mean calendar-day equity change ($)
    return_on_capital_pct: float = 0.0  # net_profit / initial capital (= total return)
    # ---- cost attribution & gross-vs-net ---------------------------------
    total_slippage: float = 0.0      # $ slippage + latency drag
    total_financing: float = 0.0     # $ short-borrow / margin / funding
    total_costs: float = 0.0         # commission + slippage + financing ($)
    gross_return_pct: float = 0.0    # return if all costs were zero (add-back approx)

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)


def _max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    return float(drawdown.min())  # negative number


def compute_report(
    equity_df: pd.DataFrame,
    trade_log: list,
    total_commission: float,
    periods_per_year: float = 365 * 24,
    risk_free: float = 0.0,
    total_slippage: float = 0.0,
    total_financing: float = 0.0,
) -> PerformanceReport:
    if equity_df.empty or len(equity_df) < 2:
        eq0 = equity_df["equity"].iloc[0] if not equity_df.empty else 0.0
        return PerformanceReport(eq0, eq0, 0, 0, 0, 0, 0, 0, 0, 0, len(trade_log), 0,
                                 total_commission)

    equity = equity_df["equity"]
    returns = equity_df["returns"].fillna(0.0)

    initial = float(equity.iloc[0])
    final = float(equity.iloc[-1])
    total_return = (final / initial - 1) * 100

    n_periods = len(returns)
    years = n_periods / periods_per_year
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and initial > 0 else 0.0

    ann_vol = float(returns.std(ddof=0) * np.sqrt(periods_per_year)) * 100

    excess = returns - risk_free / periods_per_year
    sharpe = (
        float(excess.mean() / returns.std(ddof=0) * np.sqrt(periods_per_year))
        if returns.std(ddof=0) > 0 else 0.0
    )

    # Textbook downside deviation: RMS of shortfalls below the target (0 here),
    # measured over ALL periods (not the std of the negatives subset), so it is
    # comparable to standard Sortino quotes and consistent with the full-sample
    # convention used by Sharpe above.
    target = risk_free / periods_per_year
    shortfall = np.minimum(returns - target, 0.0)
    downside_dev = float(np.sqrt(np.mean(shortfall ** 2)))
    sortino = (
        float(excess.mean() / downside_dev * np.sqrt(periods_per_year))
        if downside_dev > 0 else 0.0
    )

    mdd = _max_drawdown(equity) * 100
    calmar = cagr / abs(mdd) if mdd != 0 else 0.0
    var95 = float(-np.percentile(returns, 5)) * 100

    n_trades = len(trade_log)
    pnls = [t.get("realized_pnl", 0.0) for t in trade_log]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = (len(wins) / n_trades * 100) if n_trades else 0.0

    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # positive magnitude
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    risk_reward = (avg_win / avg_loss) if avg_loss > 0 else (
        float("inf") if avg_win > 0 else 0.0
    )

    # Average *daily* return: resample the (possibly intraday) equity to calendar
    # days and average the day-over-day percentage change. Also capture the
    # mean daily $ change for fixed-capital ("$/day") reporting.
    avg_daily_return = 0.0
    avg_daily_profit = 0.0
    try:
        daily_eq = equity.resample("1D").last().dropna()
        if len(daily_eq) >= 2:
            avg_daily_return = float(daily_eq.pct_change().dropna().mean()) * 100
            avg_daily_profit = float(daily_eq.diff().dropna().mean())
    except (TypeError, ValueError):
        # Non-datetime index (e.g. synthetic positional index) — fall back to
        # a periods-per-day approximation from per-bar returns.
        per_day = max(1.0, periods_per_year / 365.0)
        avg_daily_return = float((1 + returns.mean()) ** per_day - 1) * 100
        avg_daily_profit = (final - initial) / max(1.0, n_periods / per_day)

    net_profit = final - initial
    roc = (net_profit / initial * 100) if initial > 0 else 0.0

    # Cost attribution. Gross return adds all itemised costs back as a fraction
    # of starting capital — an approximation (it ignores the compounding the
    # costs displaced); the exact gross figure comes from a paired zero-cost run.
    total_costs = total_commission + total_slippage + total_financing
    gross_return_pct = total_return + (total_costs / initial * 100) if initial > 0 else total_return

    return PerformanceReport(
        initial_equity=initial,
        final_equity=final,
        total_return_pct=total_return,
        cagr_pct=cagr,
        annual_volatility_pct=ann_vol,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=mdd,
        calmar=calmar,
        var_95_pct=var95,
        n_trades=n_trades,
        win_rate_pct=win_rate,
        total_commission=total_commission,
        avg_daily_return_pct=avg_daily_return,
        profit_factor=profit_factor,
        risk_reward_ratio=risk_reward,
        avg_win=avg_win,
        avg_loss=avg_loss,
        net_profit=net_profit,
        avg_daily_profit=avg_daily_profit,
        return_on_capital_pct=roc,
        total_slippage=total_slippage,
        total_financing=total_financing,
        total_costs=total_costs,
        gross_return_pct=gross_return_pct,
    )


def format_report(report: PerformanceReport) -> str:
    def _pf(x):
        return "inf" if x == float("inf") else f"{x:,.2f}"

    rows = [
        ("Initial equity", f"{report.initial_equity:,.2f}"),
        ("Final equity", f"{report.final_equity:,.2f}"),
        ("Net profit", f"{report.net_profit:,.2f}"),
        ("Return on capital", f"{report.return_on_capital_pct:,.2f} %"),
        ("Avg daily profit", f"{report.avg_daily_profit:,.4f}"),
        ("Total return", f"{report.total_return_pct:,.2f} %"),
        ("Avg daily return", f"{report.avg_daily_return_pct:,.4f} %"),
        ("CAGR", f"{report.cagr_pct:,.2f} %"),
        ("Annual volatility", f"{report.annual_volatility_pct:,.2f} %"),
        ("Sharpe", f"{report.sharpe:,.2f}"),
        ("Sortino", f"{report.sortino:,.2f}"),
        ("Max drawdown", f"{report.max_drawdown_pct:,.2f} %"),
        ("Calmar", f"{report.calmar:,.2f}"),
        ("VaR (95%, 1-period)", f"{report.var_95_pct:,.2f} %"),
        ("Closed trades", f"{report.n_trades}"),
        ("Win rate", f"{report.win_rate_pct:,.1f} %"),
        ("Profit factor", _pf(report.profit_factor)),
        ("Risk/reward (avg win/loss)", _pf(report.risk_reward_ratio)),
        ("Gross return (pre-cost)", f"{report.gross_return_pct:,.2f} %"),
        ("  cost: commission", f"{report.total_commission:,.2f}"),
        ("  cost: slippage + latency", f"{report.total_slippage:,.2f}"),
        ("  cost: financing (borrow/funding)", f"{report.total_financing:,.2f}"),
        ("Total costs", f"{report.total_costs:,.2f}"),
        ("Net return (after costs)", f"{report.total_return_pct:,.2f} %"),
    ]
    try:
        from tabulate import tabulate

        return tabulate(rows, headers=["Metric", "Value"], tablefmt="github")
    except ImportError:
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"{k:<{width}} : {v}" for k, v in rows)
