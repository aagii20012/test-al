from datetime import datetime

from algotrading.core.event_queue import EventQueue
from algotrading.core.enums import Direction
from algotrading.core.events import FillEvent
from algotrading.data.historical import HistoricCSVDataHandler, make_synthetic_frames
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskManager


def _make_portfolio():
    events = EventQueue()
    frames = make_synthetic_frames(["BTCUSDT"], n_bars=10)
    data = HistoricCSVDataHandler(events, frames)
    data.update_bars()  # load first bar so prices exist
    pf = Portfolio(data, events, RiskManager(), initial_capital=10_000)
    return pf, data


def test_buy_then_sell_realizes_pnl():
    pf, data = _make_portfolio()
    dt = datetime(2023, 1, 1)

    pf.update_fill(FillEvent(dt, "BTCUSDT", Direction.BUY, 1.0, 100.0, 0.0))
    assert pf.position("BTCUSDT") == 1.0
    assert pf.cash == 10_000 - 100.0

    pf.update_fill(FillEvent(dt, "BTCUSDT", Direction.SELL, 1.0, 120.0, 0.0))
    assert pf.position("BTCUSDT") == 0.0
    assert round(pf.realized_pnl, 6) == 20.0
    assert len(pf.trade_log) == 1


def test_commission_reduces_cash():
    pf, _ = _make_portfolio()
    dt = datetime(2023, 1, 1)
    pf.update_fill(FillEvent(dt, "BTCUSDT", Direction.BUY, 1.0, 100.0, 5.0))
    assert pf.cash == 10_000 - 100.0 - 5.0
    assert pf.total_commission == 5.0


def test_average_price_on_scale_in():
    pf, _ = _make_portfolio()
    dt = datetime(2023, 1, 1)
    pf.update_fill(FillEvent(dt, "BTCUSDT", Direction.BUY, 1.0, 100.0, 0.0))
    pf.update_fill(FillEvent(dt, "BTCUSDT", Direction.BUY, 1.0, 200.0, 0.0))
    assert pf.position("BTCUSDT") == 2.0
    assert pf.avg_price["BTCUSDT"] == 150.0
