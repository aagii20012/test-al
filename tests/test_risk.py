import numpy as np

from algotrading.core.enums import Direction, SignalType
from algotrading.core.event_queue import EventQueue
from algotrading.core.events import SignalEvent
from algotrading.data.historical import HistoricCSVDataHandler, make_synthetic_frames
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager


def _ctx(risk):
    events = EventQueue()
    frames = make_synthetic_frames(["BTCUSDT"], n_bars=10)
    data = HistoricCSVDataHandler(events, frames)
    data.update_bars()
    pf = Portfolio(data, events, risk, initial_capital=10_000)
    return pf, data


def test_position_size_respects_max_position_pct():
    risk = RiskManager(RiskConfig(max_position_pct=0.1, risk_per_trade=1.0, max_leverage=10))
    pf, _ = _ctx(risk)
    price = 100.0
    order = risk.size_order(
        SignalEvent("BTCUSDT", None, SignalType.LONG), pf, price
    )
    # 10% of 10k equity / price 100 = 10 units max
    assert order.quantity <= 10.0 + 1e-9
    assert order.direction is Direction.BUY


def test_leverage_cap_trims_order():
    risk = RiskManager(RiskConfig(max_position_pct=1.0, risk_per_trade=1.0, max_leverage=0.5))
    pf, _ = _ctx(risk)
    order = risk.size_order(SignalEvent("BTCUSDT", None, SignalType.LONG), pf, 100.0)
    # gross exposure cannot exceed 0.5 * 10k = 5000 -> 50 units
    assert order.quantity <= 50.0 + 1e-9


def test_exit_signal_with_no_position_returns_none():
    risk = RiskManager()
    pf, _ = _ctx(risk)
    assert risk.size_order(SignalEvent("BTCUSDT", None, SignalType.EXIT), pf, 100.0) is None


def test_value_at_risk():
    returns = np.array([-0.05, -0.02, 0.0, 0.01, 0.03])
    var = RiskManager.value_at_risk(returns, confidence=0.8)
    assert var > 0  # VaR reported as positive loss fraction
