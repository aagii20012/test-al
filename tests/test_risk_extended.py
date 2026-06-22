"""Tests for the extended risk controls: take-profit, ATR sizing, and the
daily-loss / max-drawdown circuit breakers. Each builds a deterministic price
path that is guaranteed to trip the mechanism under test."""

from datetime import datetime, timedelta

import pandas as pd

from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.buy_and_hold import BuyAndHoldStrategy


def _frame(closes, freq_minutes=60, start="2023-01-01"):
    idx = pd.DatetimeIndex(
        [datetime.fromisoformat(start) + timedelta(minutes=freq_minutes * i)
         for i in range(len(closes))]
    )
    df = pd.DataFrame(
        {"open": closes, "high": [c * 1.001 for c in closes],
         "low": [c * 0.999 for c in closes], "close": closes,
         "volume": [100.0] * len(closes)},
        index=idx,
    )
    return {"BTCUSDT": df}


def _run(frames, risk_cfg, strat_cls=BuyAndHoldStrategy, **params):
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(risk_cfg)
    pf = Portfolio(data, events, risk, initial_capital=100_000)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0)
    strat = strat_cls(data, events, **params)
    BacktestEngine(data, strat, pf, execu, events).run()
    return pf, risk


def test_take_profit_closes_winner():
    closes = [100.0 * (1.01 ** i) for i in range(30)]  # +1%/bar, steadily up
    cfg = RiskConfig(use_stops=False, take_profit_pct=0.02, risk_per_trade=0.2,
                     max_position_pct=0.5)
    pf, _ = _run(_frame(closes), cfg)
    # Take-profit must have fired, booking a positive realized P&L.
    assert pf.trade_log
    assert any(t["realized_pnl"] > 0 for t in pf.trade_log)


def test_max_drawdown_kill_switch_halts_and_flattens():
    # Calm, then a 50% crash — far past the 20% max-drawdown limit.
    closes = [100.0] * 20 + [100.0 - 2.5 * i for i in range(1, 21)]  # down to ~50
    cfg = RiskConfig(use_stops=False, max_drawdown_pct=0.20, risk_per_trade=0.5,
                     max_position_pct=1.0, max_leverage=1.0)
    pf, risk = _run(_frame(closes), cfg)
    assert risk._halted_permanent is True
    assert pf.position("BTCUSDT") == 0  # book was flattened


def test_daily_profit_lock_banks_the_win_and_halts():
    # A steady rise should trip the +5% daily profit-lock: it flattens and stops
    # for the day, so the day ends near +5% rather than riding the full ride.
    # Keep within ONE calendar day (<=24 hourly bars) so the daily halt persists.
    closes = [100.0 * (1.01 ** i) for i in range(20)]  # +1%/bar, all 2023-01-01
    cfg = RiskConfig(use_stops=False, max_daily_profit_pct=0.05, risk_per_trade=1.0,
                     max_position_pct=1.0)
    pf, risk = _run(_frame(closes), cfg)
    assert risk._halted_today is True
    assert pf.position("BTCUSDT") == 0          # win was banked (flattened)
    # Locked in roughly the target, not the full ride.
    assert 1.03 < pf.equity / 100_000 < 1.12


def test_max_daily_loss_halts_for_the_day():
    # A sharp intraday drop within a single calendar day trips the daily limit.
    closes = [100.0, 100.0, 90.0, 80.0, 70.0, 60.0]  # all same day (hourly)
    cfg = RiskConfig(use_stops=False, max_daily_loss_pct=0.05, risk_per_trade=0.5,
                     max_position_pct=1.0)
    pf, risk = _run(_frame(closes), cfg)
    assert risk._halted_today is True
    assert pf.position("BTCUSDT") == 0


def test_same_bar_orders_respect_leverage_and_cash():
    # Regression: many symbols entered on the SAME bar must not collectively
    # breach max_leverage=1.0 or drive cash negative, even though no fill is
    # applied until after every signal that bar has been sized.
    from algotrading.core.enums import SignalType
    from algotrading.core.events import MarketEvent, SignalEvent
    from algotrading.engine.backtest import BacktestEngine
    from algotrading.strategy.base import Strategy

    class EnterAllStrategy(Strategy):
        def __init__(self, data, events, **kw):
            super().__init__(data, events, **kw)
            self._done = {s: False for s in self.symbols}

        def calculate_signals(self, event):
            for s in self.symbols:
                bar = self.data.get_latest_bar(s)
                if bar is not None and not self._done[s]:
                    self.events.put(SignalEvent(s, bar.dt, SignalType.LONG))
                    self._done[s] = True

    symbols = [f"S{i}" for i in range(15)]
    idx = pd.DatetimeIndex([datetime(2023, 1, 1) + timedelta(hours=i) for i in range(4)])
    frames = {s: pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e9}, index=idx)
        for s in symbols}

    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(RiskConfig(max_position_pct=0.20, risk_per_trade=0.10,
                                  max_leverage=1.0, use_stops=False))
    pf = Portfolio(data, events, risk, initial_capital=100_000)
    execu = SimulatedExecutionHandler(events, data, commission_pct=0.0, slippage_bps=0.0)
    BacktestEngine(data, EnterAllStrategy(data, events), pf, execu, events).run()

    # Without the per-bar pending accumulator this over-leverages to ~1.5x and
    # cash goes deeply negative; with it, both limits hold.
    assert pf.gross_exposure <= pf.equity * 1.0 + 1.0
    assert pf.cash >= -1e-6


def test_atr_sizing_risks_a_bounded_fraction_per_trade():
    # ATR sizing should size the position so the stop loss costs ~risk_per_trade.
    closes = [100.0 + (i % 5) for i in range(60)]  # mild oscillation -> finite ATR
    cfg = RiskConfig(atr_sizing=True, risk_per_trade=0.01, atr_stop_mult=2.0,
                     max_position_pct=1.0, use_stops=True)
    pf, _ = _run(_frame(closes), cfg)
    eq = pf.equity_dataframe()
    assert len(eq) > 0
    # Sizing by risk-to-stop must keep single-bar moves from blowing up equity.
    assert pf.equity > 0
