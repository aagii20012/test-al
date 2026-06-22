"""Programmatic backtest example (no CLI).

Run:  python examples/run_backtest.py
Uses synthetic data so it works offline with no API keys.
"""

from algotrading.analytics.performance import compute_report, format_report
from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler, make_synthetic_frames
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.sma_crossover import SMACrossoverStrategy
from algotrading.utils.logger import configure_logging


def main():
    configure_logging("INFO")
    symbols = ["BTCUSDT"]

    events = EventQueue()
    frames = make_synthetic_frames(symbols, n_bars=3000)
    data = HistoricCSVDataHandler(events, frames)

    risk = RiskManager(RiskConfig(max_position_pct=0.5, risk_per_trade=0.25,
                                  max_leverage=1.0, stop_loss_pct=0.08))
    portfolio = Portfolio(data, events, risk, initial_capital=100_000)
    execution = SimulatedExecutionHandler(events, data, commission_pct=0.001, slippage_bps=2)
    strategy = SMACrossoverStrategy(data, events, fast=20, slow=50)

    BacktestEngine(data, strategy, portfolio, execution, events).run()

    report = compute_report(
        portfolio.equity_dataframe(), portfolio.trade_log,
        portfolio.total_commission, periods_per_year=365 * 24,
    )
    print("\n=== Backtest performance ===")
    print(format_report(report))


if __name__ == "__main__":
    main()
