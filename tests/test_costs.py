"""Tests for the full cost stack: commission (existing), slippage tracking,
market impact, short-borrow/financing, and gross-vs-net reporting."""

from datetime import datetime, timedelta

import pandas as pd

from algotrading.core.enums import Direction, OrderType, SignalType
from algotrading.core.event_queue import EventQueue
from algotrading.core.events import FillEvent, MarketEvent, OrderEvent, SignalEvent
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskConfig, RiskManager
from algotrading.strategy.base import Strategy
from algotrading.strategy.buy_and_hold import BuyAndHoldStrategy


def _frame(closes, vol=1e9):
    idx = pd.DatetimeIndex([datetime(2023, 1, 1) + timedelta(hours=i) for i in range(len(closes))])
    df = pd.DataFrame({"open": closes, "high": [c * 1.001 for c in closes],
                       "low": [c * 0.999 for c in closes], "close": closes,
                       "volume": [vol] * len(closes)}, index=idx)
    return {"BTCUSDT": df}


class _ShortAndHold(Strategy):
    def __init__(self, data, events, **kw):
        super().__init__(data, events, **kw)
        self._done = False

    def calculate_signals(self, event):
        if not self._done:
            bar = self.data.get_latest_bar(self.symbols[0])
            if bar is not None:
                self.events.put(SignalEvent(self.symbols[0], bar.dt, SignalType.SHORT))
                self._done = True


def _run(strat_cls, frames, risk_cfg, *, financing_apr=0.0, slippage_bps=0.0,
         commission_pct=0.0, **kw):
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(risk_cfg)
    pf = Portfolio(data, events, risk, initial_capital=10_000,
                   financing_apr=financing_apr, periods_per_year=365 * 24)
    execu = SimulatedExecutionHandler(events, data, commission_pct=commission_pct,
                                      slippage_bps=slippage_bps, **kw)
    BacktestEngine(data, strat_cls(data, events), pf, execu, events).run()
    return pf


# ---- financing / short borrow ----------------------------------------------
def test_short_position_accrues_financing():
    frames = _frame([100.0] * 30)   # flat price; isolate the carry cost
    cfg = RiskConfig(use_stops=False, allow_short=True, risk_per_trade=0.5,
                     max_position_pct=1.0)
    pf = _run(_ShortAndHold, frames, cfg, financing_apr=0.10)
    assert pf.total_financing > 0           # borrow cost was charged
    # On a flat price the only thing moving equity down is financing.
    assert pf.equity < 10_000


def test_no_financing_when_rate_zero_or_long_only_spot():
    frames = _frame([100.0] * 30)
    cfg = RiskConfig(use_stops=False, risk_per_trade=0.5, max_position_pct=1.0)
    # Long-only spot with the default 0 rate must charge nothing.
    pf = _run(BuyAndHoldStrategy, frames, cfg, financing_apr=0.0)
    assert pf.total_financing == 0.0


# ---- slippage tracking ------------------------------------------------------
def test_slippage_is_tracked_and_not_double_charged():
    frames = _frame([100.0] * 10)
    cfg = RiskConfig(use_stops=False, risk_per_trade=0.5, max_position_pct=1.0)
    pf = _run(BuyAndHoldStrategy, frames, cfg, slippage_bps=50.0)
    assert pf.total_financing == 0.0
    assert pf.total_slippage > 0            # slippage recorded as a $ amount
    # Slippage is embedded in fill_price; cash should reflect it exactly once.
    # Buying ~ full equity of notional at +50bps: equity drops ~ the slippage.
    assert 9_900 < pf.equity < 10_000


def test_market_impact_worsens_fill_price_with_size():
    # Same order; with a nonzero impact coefficient the buy fills higher.
    frames = _frame([100.0, 100.0], vol=1000.0)
    def fill_price(impact):
        events = EventQueue()
        data = HistoricCSVDataHandler(events, frames)
        data.update_bars()
        list(events.drain())
        execu = SimulatedExecutionHandler(events, data, commission_pct=0.0,
                                          slippage_bps=0.0, impact_coeff_bps=impact)
        execu.execute_order(OrderEvent("BTCUSDT", OrderType.MARKET, 100.0, Direction.BUY))
        fills = [e for e in events.drain() if isinstance(e, FillEvent)]
        return fills[0].fill_price
    base = fill_price(0.0)
    impacted = fill_price(1000.0)           # 100/1000 = 10% participation
    assert impacted > base                  # impact pushes the buy price up


# ---- gross vs net -----------------------------------------------------------
def test_gross_vs_net_decomposition():
    from algotrading.analytics.performance import compute_report
    frames = _frame([100.0 + (i % 7) for i in range(200)])
    cfg = RiskConfig(use_stops=False, risk_per_trade=0.3, max_position_pct=0.5)
    pf = _run(BuyAndHoldStrategy, frames, cfg, slippage_bps=10.0, commission_pct=0.001)
    rep = compute_report(pf.equity_dataframe(), pf.trade_log, pf.total_commission,
                         total_slippage=pf.total_slippage,
                         total_financing=pf.total_financing)
    # total_costs reconciles with the components, and gross >= net once costs > 0.
    assert abs(rep.total_costs - (pf.total_commission + pf.total_slippage
                                  + pf.total_financing)) < 1e-9
    assert rep.total_costs > 0
    assert rep.gross_return_pct >= rep.total_return_pct
