"""End-to-end smoke test: the full event pipeline runs and produces a report."""

from algotrading.analytics.performance import compute_report
from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler, make_synthetic_frames
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.sma_crossover import SMACrossoverStrategy
from algotrading.strategy.buy_and_hold import BuyAndHoldStrategy


def _run(strategy_cls, use_stops=True, **params):
    events = EventQueue()
    frames = make_synthetic_frames(["BTCUSDT"], n_bars=500, seed=7)
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(RiskConfig(max_position_pct=0.5, risk_per_trade=0.25,
                                  use_stops=use_stops))
    pf = Portfolio(data, events, risk, initial_capital=100_000)
    execution = SimulatedExecutionHandler(events, data)
    strat = strategy_cls(data, events, **params)
    BacktestEngine(data, strat, pf, execution, events).run()
    return pf


def test_sma_backtest_runs_and_records_equity():
    pf = _run(SMACrossoverStrategy, fast=10, slow=30)
    eq = pf.equity_dataframe()
    assert len(eq) > 0
    assert "equity" in eq.columns
    assert pf.equity > 0


def test_buyhold_takes_one_position():
    # Disable stops so the benchmark genuinely holds through drawdowns.
    pf = _run(BuyAndHoldStrategy, use_stops=False)
    assert pf.position("BTCUSDT") > 0


def test_report_fields_present():
    pf = _run(SMACrossoverStrategy, fast=10, slow=30)
    report = compute_report(pf.equity_dataframe(), pf.trade_log, pf.total_commission)
    d = report.as_dict()
    for key in ["sharpe", "max_drawdown_pct", "total_return_pct", "var_95_pct"]:
        assert key in d
