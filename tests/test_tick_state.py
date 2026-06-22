"""State persistence for the run-once / cloud 'tick' mode.

A cron bot restarts the process every cycle, so portfolio + risk + strategy
state must survive a JSON round-trip exactly. These tests run a short backtest
to populate real state, serialise it through json.dumps/loads, restore it into
fresh objects, and assert nothing changed.
"""

import json
from datetime import datetime, timedelta

import pandas as pd

from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.momentum import MomentumStrategy


def _trending_frame(n=200, start="2023-01-01"):
    # A rising-then-falling path so momentum actually opens and flips a position.
    closes = []
    price = 100.0
    for i in range(n):
        price *= 1.01 if i < n // 2 else 0.99
        closes.append(price)
    idx = pd.DatetimeIndex(
        [datetime.fromisoformat(start) + timedelta(hours=i) for i in range(n)])
    df = pd.DataFrame(
        {"open": closes, "high": [c * 1.002 for c in closes],
         "low": [c * 0.998 for c in closes], "close": closes,
         "volume": [1000.0] * n}, index=idx)
    return {"BTCUSDT": df}


def _run_to_populate():
    frames = _trending_frame()
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(RiskConfig(atr_sizing=True, risk_per_trade=0.01,
                                  atr_stop_mult=2.5, max_position_pct=0.5,
                                  max_daily_loss_pct=0.02, max_drawdown_pct=0.25))
    pf = Portfolio(data, events, risk, initial_capital=10_000)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.001, slippage_bps=2.0)
    strat = MomentumStrategy(data, events, lookback=24, threshold=0.5)
    BacktestEngine(data, strat, pf, execu, events).run()
    return pf, risk, strat


def test_portfolio_state_survives_json_roundtrip():
    pf, _, _ = _run_to_populate()
    assert pf.trade_log, "test needs at least one trade to be meaningful"

    blob = json.loads(json.dumps(pf.dump_state()))  # must be JSON-serialisable

    events = EventQueue()
    data = HistoricCSVDataHandler(events, _trending_frame())
    pf2 = Portfolio(data, events, RiskManager(RiskConfig()), initial_capital=1.0)
    pf2.load_state(blob)

    assert pf2.cash == pf.cash
    assert pf2.initial_capital == pf.initial_capital
    assert dict(pf2.positions) == dict(pf.positions)
    assert pf2.total_commission == pf.total_commission
    assert len(pf2.trade_log) == len(pf.trade_log)
    assert len(pf2.equity_curve) == len(pf.equity_curve)
    # dt fields must come back as datetimes, not strings.
    assert isinstance(pf2.equity_curve[0]["dt"], datetime)


def test_risk_state_survives_json_roundtrip():
    _, risk, _ = _run_to_populate()
    blob = json.loads(json.dumps(risk.dump_state()))

    risk2 = RiskManager(RiskConfig())
    risk2.load_state(blob)

    assert risk2._halted_today == risk._halted_today
    assert risk2._halted_permanent == risk._halted_permanent
    assert risk2._day == risk._day
    assert risk2._peak_equity == risk._peak_equity
    assert risk2._open == risk._open


def test_strategy_position_memory_survives_roundtrip():
    _, _, strat = _run_to_populate()
    blob = json.loads(json.dumps(strat.dump_state()))

    events = EventQueue()
    data = HistoricCSVDataHandler(events, _trending_frame())
    strat2 = MomentumStrategy(data, events, lookback=24, threshold=0.5)
    strat2.load_state(blob)
    assert strat2._pos == strat._pos


def test_fresh_load_state_is_noop_safe():
    # Loading an empty/partial blob must not crash (first-ever run path).
    events = EventQueue()
    data = HistoricCSVDataHandler(events, _trending_frame())
    strat = MomentumStrategy(data, events, lookback=24)
    strat.load_state({})
    assert all(v == 0 for v in strat._pos.values())


class _FakeBook:
    """Minimal portfolio stand-in exposing position(symbol)."""
    def __init__(self, positions):
        self._p = positions

    def position(self, s):
        return self._p.get(s, 0.0)


def test_sync_positions_heals_desync_after_forced_exit():
    # Reproduces the wedge bug: a stop / circuit-breaker flattened the book
    # (position 0) but the strategy still THINKS it is long (_pos=1). Without
    # reconciliation it would never re-enter; sync_positions must heal it.
    events = EventQueue()
    data = HistoricCSVDataHandler(events, _trending_frame())
    strat = MomentumStrategy(data, events, lookback=24)
    strat._pos["BTCUSDT"] = 1            # stale "I'm long" memory

    strat.sync_positions(_FakeBook({"BTCUSDT": 0.0}))   # real book is flat
    assert strat._pos["BTCUSDT"] == 0    # healed -> free to re-enter

    # And it maps an actual short back to -1, an actual long to +1.
    strat.sync_positions(_FakeBook({"BTCUSDT": -0.5}))
    assert strat._pos["BTCUSDT"] == -1
    strat.sync_positions(_FakeBook({"BTCUSDT": 2.0}))
    assert strat._pos["BTCUSDT"] == 1


def test_dumped_history_is_capped():
    # The committed checkpoint must not grow without bound.
    pf, _, _ = _run_to_populate()
    # Force the lists past the caps and confirm dump truncates to the tail.
    pf.equity_curve = pf.equity_curve * 3000
    blob = pf.dump_state()
    assert len(blob["equity_curve"]) <= Portfolio._MAX_EQUITY_ROWS
    assert len(blob["trade_log"]) <= Portfolio._MAX_TRADE_ROWS
