"""The HTML dashboard generates a valid, self-contained file."""

import os

from algotrading.analytics.dashboard import generate_html_report
from algotrading.analytics.performance import compute_report
from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler, make_synthetic_frames
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskManager
from algotrading.strategy.sma_crossover import SMACrossoverStrategy


def test_dashboard_writes_self_contained_html(tmp_path):
    events = EventQueue()
    frames = make_synthetic_frames(["BTCUSDT"], n_bars=400, seed=3)
    data = HistoricCSVDataHandler(events, frames)
    pf = Portfolio(data, events, RiskManager(), initial_capital=100_000)
    execution = SimulatedExecutionHandler(events, data)
    strat = SMACrossoverStrategy(data, events, fast=10, slow=30)
    BacktestEngine(data, strat, pf, execution, events).run()

    report = compute_report(pf.equity_dataframe(), pf.trade_log, pf.total_commission)
    out = tmp_path / "report.html"
    generate_html_report(pf.equity_dataframe(), pf.trade_log, report, str(out))

    assert out.exists() and os.path.getsize(out) > 1000
    html = out.read_text(encoding="utf-8")
    # Self-contained: charts inline, no external asset references.
    assert "<svg" in html and "Equity curve" in html
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html  # no external scripts/images
