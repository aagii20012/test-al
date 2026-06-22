"""Tests for realistic execution: latency (next-bar-open fill), partial fills,
and minimum-notional rejection. Each builds a deterministic scenario so the
mechanism is exercised unambiguously."""

from datetime import datetime, timedelta

import pandas as pd

from algotrading.core.enums import Direction, OrderType
from algotrading.core.event_queue import EventQueue
from algotrading.core.events import FillEvent, MarketEvent, OrderEvent
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.execution.simulated import SimulatedExecutionHandler


def _frame(rows):
    """rows: list of (open, high, low, close, volume)."""
    idx = pd.DatetimeIndex(
        [datetime(2023, 1, 1) + timedelta(hours=i) for i in range(len(rows))]
    )
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    return {"BTCUSDT": df}


def _drain_fills(events):
    fills = []
    for e in events.drain():
        if isinstance(e, FillEvent):
            fills.append(e)
    return fills


def test_next_open_fill_uses_next_bar_open_not_decision_close():
    # Decide on bar 0 (close=100); fill must occur at bar 1's open (=110).
    frames = _frame([(100, 100, 100, 100, 1e9), (110, 110, 110, 110, 1e9)])
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0,
                                      fill_at="next_open")

    data.update_bars()                      # bar 0 now latest (close 100)
    list(events.drain())                    # clear the MarketEvent
    execu.execute_order(OrderEvent("BTCUSDT", OrderType.MARKET, 1.0, Direction.BUY))
    assert _drain_fills(events) == []       # deferred: nothing fills on bar 0

    data.update_bars()                      # bar 1 now latest (open 110)
    list(e for e in events.drain() if isinstance(e, MarketEvent))  # clear market event
    fills = execu.on_market(MarketEvent(dt=frames["BTCUSDT"].index[1].to_pydatetime()))
    assert len(fills) == 1
    assert abs(fills[0].fill_price - 110.0) < 1e-9   # filled at next bar's OPEN


def test_partial_fill_caps_at_participation_of_volume():
    # Want 100 units, but a bar only trades 200 units and we cap at 5% = 10/bar.
    frames = _frame([(100, 100, 100, 100, 200.0)] * 6)
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0,
                                      participation_rate=0.05, max_working_bars=10)
    data.update_bars()
    list(events.drain())
    execu.execute_order(OrderEvent("BTCUSDT", OrderType.MARKET, 100.0, Direction.BUY))
    first = _drain_fills(events)
    assert len(first) == 1
    assert abs(first[0].quantity - 10.0) < 1e-9      # 5% of 200 = 10 units this bar

    # Subsequent bars keep working the remainder, 10 units at a time.
    total = first[0].quantity
    for _ in range(5):
        data.update_bars()
        list(e for e in events.drain() if isinstance(e, MarketEvent))
        total += sum(f.quantity for f in execu.on_market(MarketEvent(dt=None)))
    assert 50.0 - 1e-9 <= total <= 60.0 + 1e-9       # ~10/bar across the bars worked


def test_min_notional_rejects_dust_orders():
    # A tiny order below the $10 min notional must be rejected, producing no fill.
    frames = _frame([(100, 100, 100, 100, 1e9)] * 2)
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0,
                                      min_notional=10.0)
    data.update_bars()
    list(events.drain())
    execu.execute_order(OrderEvent("BTCUSDT", OrderType.MARKET, 0.05, Direction.BUY))  # $5
    assert _drain_fills(events) == []                # rejected: below $10 notional


def test_next_open_fill_is_marked_in_the_same_bar():
    # Regression: a position filled at bar N+1's OPEN must be reflected in that
    # bar's equity mark (not lag a bar). Decide on bar0 (close 100); fill at
    # bar1 open 100; bar1 then rises to close 120. Equity at the fill bar must
    # already show the gain.
    from algotrading.engine.backtest import BacktestEngine
    from algotrading.portfolio.portfolio import Portfolio
    from algotrading.risk.risk_manager import RiskConfig, RiskManager
    from algotrading.strategy.buy_and_hold import BuyAndHoldStrategy

    frames = _frame([(100, 100, 100, 100, 1e9),   # bar0: decide
                     (100, 120, 100, 120, 1e9),   # bar1: fill@open 100, close 120
                     (120, 120, 120, 120, 1e9)])  # bar2
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(RiskConfig(use_stops=False, risk_per_trade=1.0,
                                  max_position_pct=1.0, max_leverage=1.0))
    pf = Portfolio(data, events, risk, initial_capital=100_000)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0,
                                      fill_at="next_open")
    strat = BuyAndHoldStrategy(data, events)
    BacktestEngine(data, strat, pf, execu, events).run()
    eq = pf.equity_dataframe()["equity"].tolist()
    # eq[1] is the fill bar: bought ~1000 @ 100, marked @ 120 -> ~+20%.
    assert eq[1] > 100_000 * 1.15


def test_close_mode_default_is_unchanged():
    # Default (close, full fill) must fill immediately and fully at the close.
    frames = _frame([(100, 100, 100, 105, 1e9)] * 2)
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0)
    data.update_bars()
    list(events.drain())
    execu.execute_order(OrderEvent("BTCUSDT", OrderType.MARKET, 2.0, Direction.BUY))
    fills = _drain_fills(events)
    assert len(fills) == 1
    assert abs(fills[0].quantity - 2.0) < 1e-9
    assert abs(fills[0].fill_price - 105.0) < 1e-9   # filled at the close
