"""Programmatic live/testnet trading example.

Prerequisites:
  * pip install python-binance
  * config/config.yaml with testnet api_key/api_secret (or env vars)

Run:  python examples/run_live.py
Stop with Ctrl-C. Starts on TESTNET by default — verify before going real.
"""

from algotrading.core.event_queue import EventQueue
from algotrading.data.live import LiveDataHandler
from algotrading.engine.live import LiveEngine
from algotrading.exchange.binance import BinanceExchange
from algotrading.execution.live import LiveExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.sma_crossover import SMACrossoverStrategy
from algotrading.utils.config import load_config
from algotrading.utils.logger import configure_logging


def main():
    cfg = load_config("config/config.yaml")
    configure_logging(cfg.log_level)

    if not cfg.api_key:
        raise SystemExit("Set api_key/api_secret in config/config.yaml first.")

    symbols = ["BTCUSDT"]
    exchange = BinanceExchange(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)

    events = EventQueue()
    data = LiveDataHandler(events, exchange, symbols, interval="1m")
    risk = RiskManager(RiskConfig(**(cfg.risk or {})))
    portfolio = Portfolio(data, events, risk, initial_capital=cfg.initial_capital)
    execution = LiveExecutionHandler(events, exchange)
    strategy = SMACrossoverStrategy(data, events, fast=20, slow=50)

    LiveEngine(data, strategy, portfolio, execution, events).run()


if __name__ == "__main__":
    main()
