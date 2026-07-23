"""Append-only fill ledger + lifecycle legs (Step 6, Decision 2).

Two additive audit structures on the portfolio, neither of which disturbs the
existing cash / position / realized-P&L accounting:

  * ``fills`` — exactly ONE record per real exchange fill, tagged with a
    monotonic ``fill_id`` that survives restarts. Commission and slippage are
    recorded here, ONCE.
  * ``legs``  — each fill decomposed into position-lifecycle legs. Open/scale-in
    -> one OPEN leg; reduce/close -> one CLOSE leg; a reversal through zero ->
    a CLOSE leg AND an OPEN leg, both citing the SAME parent ``fill_id``.

The load-bearing guarantee (Decision 2): a reversal is still ONE execution —
one fill record, one commission, one slippage figure — never two fabricated
exchange fills.
"""

from __future__ import annotations

import json
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
    symbols = [SYM]

    def get_latest_bar(self, symbol):
        return None

    def get_latest_bars(self, symbol, n=1):
        return []


def _portfolio() -> Portfolio:
    return Portfolio(_NoData(), EventQueue(), RiskManager(), initial_capital=100_000.0)


def _fill(direction, qty, price, *, commission=0.0, slippage=0.0) -> FillEvent:
    return FillEvent(dt=_DT, symbol=SYM, direction=direction, quantity=qty,
                     fill_price=price, commission=commission,
                     slippage_cost=slippage, exchange="SIM")


def test_each_fill_gets_one_record_with_monotonic_id():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 2, 100.0))
    pf.update_fill(_fill(Direction.BUY, 3, 110.0))
    pf.update_fill(_fill(Direction.SELL, 5, 120.0))

    assert len(pf.fills) == 3
    assert [f["fill_id"] for f in pf.fills] == [0, 1, 2]     # monotonic, gap-free
    assert pf._fill_seq == 3


def test_open_then_full_close_emits_open_then_close_legs():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 4, 100.0))           # open long 4
    pf.update_fill(_fill(Direction.SELL, 4, 130.0))          # close long 4

    kinds = [(l["leg"], l["qty"]) for l in pf.legs]
    assert kinds == [("OPEN", 4), ("CLOSE", 4)]
    open_leg, close_leg = pf.legs
    assert open_leg["fill_id"] == 0 and open_leg["entry_price"] == 100.0
    assert close_leg["fill_id"] == 1
    assert close_leg["entry_price"] == 100.0                 # measured vs the open
    assert close_leg["exit_price"] == 130.0
    assert close_leg["realized_pnl"] == pytest.approx(120.0)


def test_scale_in_emits_open_leg_only_no_close():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 4, 100.0))           # open
    pf.update_fill(_fill(Direction.BUY, 6, 110.0))           # scale in

    assert [l["leg"] for l in pf.legs] == ["OPEN", "OPEN"]
    assert pf.legs[1]["qty"] == 6                            # only the added lot
    assert pf.legs[1]["entry_price"] == 110.0                # opened at fill price


def test_partial_reduction_emits_close_leg_only_no_open():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 10, 100.0))          # open long 10
    pf.update_fill(_fill(Direction.SELL, 4, 130.0))          # reduce -> long 6

    assert [l["leg"] for l in pf.legs] == ["OPEN", "CLOSE"]
    assert pf.legs[1]["qty"] == 4                            # only the closed part
    assert pf.legs[1]["realized_pnl"] == pytest.approx(120.0)


def test_reversal_is_one_fill_two_linked_legs():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 5, 100.0, commission=1.0, slippage=0.5))
    pf.update_fill(_fill(Direction.SELL, 8, 120.0, commission=2.0, slippage=0.8))

    # ONE real fill for the reversal -> ONE fill record, one commission/slippage.
    assert len(pf.fills) == 2
    reversal = pf.fills[1]
    assert reversal["fill_id"] == 1
    assert reversal["qty"] == 8                              # the true fill size
    assert reversal["commission"] == 2.0
    assert reversal["slippage_cost"] == 0.8
    assert pf.total_commission == pytest.approx(3.0)         # never double-counted

    # ...but TWO legs, a CLOSE + an OPEN, both citing that one fill_id.
    reversal_legs = [l for l in pf.legs if l["fill_id"] == 1]
    assert len(reversal_legs) == 2
    by_kind = {l["leg"]: l for l in reversal_legs}
    assert by_kind["CLOSE"]["qty"] == 5                      # min(|prev|, |fill|)
    assert by_kind["CLOSE"]["entry_price"] == 100.0
    assert by_kind["CLOSE"]["exit_price"] == 120.0
    assert by_kind["CLOSE"]["realized_pnl"] == pytest.approx(100.0)
    assert by_kind["OPEN"]["qty"] == 3                       # the residual short
    assert by_kind["OPEN"]["entry_price"] == 120.0          # residual @ fill price
    # Legs never carry commission/slippage — those live once on the fill record.
    assert "commission" not in by_kind["OPEN"]
    assert "commission" not in by_kind["CLOSE"]


def test_ledger_survives_json_round_trip_and_ids_stay_monotonic():
    pf = _portfolio()
    pf.update_fill(_fill(Direction.BUY, 5, 100.0))
    pf.update_fill(_fill(Direction.SELL, 8, 120.0))         # reversal -> id 1

    dumped = json.loads(json.dumps(pf.dump_state(), default=str))
    restored = _portfolio()
    restored.load_state(dumped)

    assert restored._fill_seq == pf._fill_seq == 2
    assert [f["fill_id"] for f in restored.fills] == [0, 1]
    assert len(restored.legs) == len(pf.legs)

    # The NEXT fill on the restored book continues the sequence without reuse.
    restored.update_fill(_fill(Direction.BUY, 3, 110.0))    # close the short
    assert restored.fills[-1]["fill_id"] == 2
    assert restored._fill_seq == 3


def test_fill_seq_resumes_past_capped_fills_tail():
    # If the fills tail is capped, ids must still resume from the high-water mark
    # (fill_seq), not from len(fills), so ids never collide after truncation.
    pf = _portfolio()
    dumped = pf.dump_state()
    dumped["fill_seq"] = 500          # pretend 500 fills happened, tail rolled off
    dumped["fills"] = []
    restored = _portfolio()
    restored.load_state(dumped)
    restored.update_fill(_fill(Direction.BUY, 1, 100.0))
    assert restored.fills[-1]["fill_id"] == 500
