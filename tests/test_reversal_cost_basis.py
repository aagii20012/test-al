"""Minimal reversal cost-basis correction (Decision 2).

Generation 1 defect: when a single fill REVERSED a position through zero (e.g.
long 5, then sell 8 -> short 3), the residual opposite position kept the stale
pre-reversal cost basis. The very next reduce/close then mis-measured realized
P&L against an entry price that no longer existed (the "$71.87 stale-basis"
error).

The corrected accounting (Decision 2), from ONE real fill at ONE real price — no
fabricated second exchange fill:
  * close_quantity = min(|previous_position|, |signed_fill|)
  * realized P&L is booked ONLY on close_quantity, at the OLD basis
  * if the position crosses through zero, the residual opens at the ACTUAL fill
    price, so the residual's avg_price = fill price
  * it is ONE execution: one commission, one slippage figure, one trade record
"""

from __future__ import annotations

from datetime import datetime

import pytest

from algotrading.core.enums import Direction
from algotrading.core.events import FillEvent
from algotrading.core.event_queue import EventQueue
from algotrading.portfolio.portfolio import Portfolio
from algotrading.risk.risk_manager import RiskManager

SYM = "BTCUSDT"
_DT = datetime(2023, 1, 1)


class _NoData:
    """Data stub: no bars, so mark-to-market falls back to avg_price."""
    symbols = [SYM]

    def get_latest_bar(self, symbol):
        return None

    def get_latest_bars(self, symbol, n=1):
        return []


def _portfolio() -> Portfolio:
    return Portfolio(_NoData(), EventQueue(), RiskManager(), initial_capital=100_000.0)


def _fill(direction: Direction, qty: float, price: float, *, commission=0.0,
          slippage=0.0) -> FillEvent:
    return FillEvent(dt=_DT, symbol=SYM, direction=direction, quantity=qty,
                     fill_price=price, commission=commission,
                     slippage_cost=slippage, exchange="SIM")


def test_long_to_short_reversal_resets_basis_to_fill_price():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 5, 100.0))          # open long 5 @ 100
    assert pf.avg_price[SYM] == 100.0

    pf.update_fill(_fill(Direction.SELL, 8, 120.0))         # reverse -> short 3
    # Only the 5 closed shares realise P&L, at the OLD basis: (120-100)*5.
    assert pf.positions[SYM] == -3
    assert pf.realized_pnl == pytest.approx(100.0)
    # Residual short's basis is THIS fill's price, not the stale 100.
    assert pf.avg_price[SYM] == 120.0

    # Closing the short at 110 must measure P&L against 120 (correct = +30),
    # NOT against the stale 100 (buggy = -30). Total 130, not 70.
    pf.update_fill(_fill(Direction.BUY, 3, 110.0))          # close short 3 @ 110
    assert pf.positions[SYM] == 0
    assert pf.realized_pnl == pytest.approx(130.0)
    assert pf.avg_price[SYM] == 0.0


def test_short_to_long_reversal_resets_basis_to_fill_price():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.SELL, 5, 100.0))         # open short 5 @ 100
    assert pf.avg_price[SYM] == 100.0

    pf.update_fill(_fill(Direction.BUY, 8, 80.0))           # reverse -> long 3
    # Short closed cheaper -> profit: (80-100)*5*(-1) = +100.
    assert pf.positions[SYM] == 3
    assert pf.realized_pnl == pytest.approx(100.0)
    assert pf.avg_price[SYM] == 80.0                        # residual long @ fill

    pf.update_fill(_fill(Direction.SELL, 3, 90.0))          # close long 3 @ 90
    assert pf.positions[SYM] == 0
    assert pf.realized_pnl == pytest.approx(130.0)          # +100 then +30


def test_partial_reduction_keeps_basis_unchanged():
    # A reduction that does NOT cross zero must leave the basis alone (the
    # discriminator between "reduce" and "reverse" is prev_qty * new_qty).
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 10, 100.0))         # long 10 @ 100
    pf.update_fill(_fill(Direction.SELL, 4, 130.0))         # reduce -> long 6
    assert pf.positions[SYM] == 6
    assert pf.realized_pnl == pytest.approx(120.0)          # (130-100)*4
    assert pf.avg_price[SYM] == 100.0                       # basis unchanged

    pf.update_fill(_fill(Direction.SELL, 6, 90.0))          # close remaining
    assert pf.positions[SYM] == 0
    assert pf.realized_pnl == pytest.approx(60.0)           # 120 + (90-100)*6


def test_same_direction_increase_still_weighted_averages():
    # Regression guard: the new reversal branch must not disturb the ordinary
    # add-to-position weighted-average cost basis.
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 4, 100.0))
    pf.update_fill(_fill(Direction.BUY, 6, 110.0))          # add -> long 10
    assert pf.positions[SYM] == 10
    assert pf.avg_price[SYM] == pytest.approx(106.0)        # (400 + 660) / 10
    assert pf.realized_pnl == 0.0                           # no close, no P&L


def test_reversal_is_a_single_execution_not_two():
    # Decision 2: one real fill -> one commission, one slippage figure, one
    # trade record. The reversal must never be counted as two executions.
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 5, 100.0, commission=1.0, slippage=0.5))
    trades_before = len(pf.trade_log)

    pf.update_fill(_fill(Direction.SELL, 8, 120.0, commission=2.0, slippage=0.8))
    assert pf.total_commission == pytest.approx(3.0)        # 1.0 + 2.0, once each
    assert pf.total_slippage == pytest.approx(1.3)          # 0.5 + 0.8, once each
    assert len(pf.trade_log) == trades_before + 1           # exactly one close leg
