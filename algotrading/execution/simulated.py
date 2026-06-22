"""Simulated execution for backtesting — with realistic frictions.

Models the costs and constraints that most often make a backtest lie:
  * commission       — percentage fee per trade (taker by default),
  * slippage         — adverse price move vs. the reference price, in bps,
  * latency          — decide on bar N's close, fill at bar N+1's *open*
                       (`fill_at="next_open"`); this removes the optimistic
                       "fill exactly where I decided" assumption,
  * partial fills    — a single bar can only absorb a fraction of its traded
                       volume (`participation_rate`); larger orders are worked
                       across subsequent bars and the unfilled remainder is
                       cancelled after `max_working_bars`,
  * minimum notional — exchanges reject dust orders (`min_notional`), which is a
                       real and binding constraint for very small accounts.

All frictions are pessimistic and OFF by default (`fill_at="close"`,
`participation_rate=1.0`, `latency_bps=0`, `min_notional=0`) so existing
behaviour and tests are unchanged; the research scripts switch them on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..core.enums import Direction
from ..core.events import FillEvent, OrderEvent
from ..core.event_queue import EventQueue
from ..data.base import DataHandler
from ..utils.logger import get_logger
from .base import ExecutionHandler

log = get_logger(__name__)


@dataclass
class _Working:
    """An order (or remainder) still being executed."""
    symbol: str
    direction: Direction
    remaining: float
    bars_left: int


class SimulatedExecutionHandler(ExecutionHandler):
    def __init__(
        self,
        events: EventQueue,
        data: DataHandler,
        commission_pct: float = 0.001,   # 0.10% taker fee (Binance spot default)
        slippage_bps: float = 1.0,       # adverse slippage in basis points
        *,
        fill_at: str = "close",          # "close" | "next_open"
        latency_bps: float = 0.0,        # extra adverse bps to model decision lag
        participation_rate: float = 1.0, # max fraction of a bar's volume we can take
        max_working_bars: int = 3,       # bars to keep working an unfilled remainder
        min_notional: float = 0.0,       # reject fills below this $ value (0 = off)
        impact_coeff_bps: float = 0.0,   # market impact: extra bps at 100% participation
    ):
        if fill_at not in ("close", "next_open"):
            raise ValueError("fill_at must be 'close' or 'next_open'")
        self.events = events
        self.data = data
        self.commission_pct = commission_pct
        self.slippage_bps = slippage_bps
        self.fill_at = fill_at
        self.latency_bps = latency_bps
        self.participation_rate = participation_rate
        self.max_working_bars = int(max_working_bars)
        self.min_notional = float(min_notional)
        self.impact_coeff_bps = float(impact_coeff_bps)
        self._working: List[_Working] = []

    # ---- order intake ----------------------------------------------------
    def execute_order(self, order: OrderEvent) -> None:
        if self.fill_at == "next_open":
            # Defer: the decision was made on this bar's close; it cannot fill
            # until the next bar arrives (see on_market).
            self._working.append(_Working(order.symbol, order.direction,
                                           abs(order.quantity), self.max_working_bars))
            return
        # Immediate (close) fill, possibly partial under a participation cap.
        work = _Working(order.symbol, order.direction, abs(order.quantity),
                        self.max_working_bars)
        fill = self._fill_working(work, ref="close")
        if fill is not None:
            self.events.put(fill)
        if work.remaining > 1e-12 and self.participation_rate < 1.0:
            work.bars_left -= 1
            self._working.append(work)

    # ---- per-bar working-order flush (latency / partial remainders) ------
    def on_market(self, event) -> List[FillEvent]:
        """Fill orders deferred from a prior bar against the new bar.

        Returns the resulting fills so the engine can apply them to the
        portfolio *before* it marks-to-market this bar — otherwise a position
        entered at this bar's open would be missing from this bar's equity mark
        and its P&L would leak into the next bar's return (biasing Sharpe/vol).
        """
        if not self._working:
            return []
        ref = "open" if self.fill_at == "next_open" else "close"
        fills: List[FillEvent] = []
        still: List[_Working] = []
        for work in self._working:
            fill = self._fill_working(work, ref=ref)
            if fill is not None:
                fills.append(fill)
            work.bars_left -= 1
            if work.remaining > 1e-12 and work.bars_left > 0:
                still.append(work)
            elif work.remaining > 1e-12:
                log.warning("Cancelling unfilled remainder %.8f %s (%s) — no liquidity",
                            work.remaining, work.symbol, work.direction.value)
        self._working = still
        return fills

    # ---- core fill logic -------------------------------------------------
    def _fill_working(self, work: _Working, ref: str) -> Optional[FillEvent]:
        bar = self.data.get_latest_bar(work.symbol)
        if bar is None:
            return None
        ref_price = bar.open if ref == "open" else bar.close

        # How much of this bar's volume can we realistically take?
        if self.participation_rate < 1.0:
            available = self.participation_rate * bar.volume
        else:
            available = work.remaining
        qty = min(work.remaining, max(0.0, available))
        if qty <= 0:
            return None

        # Adverse price move against us: fixed slippage + latency, PLUS a
        # size-aware market-impact term that grows with how much of the bar's
        # volume this fill consumes (book consumption). impact_coeff_bps is the
        # extra cost at 100% participation; ~0 for a tiny account, dominant at
        # institutional notional. Default 0 keeps behaviour unchanged.
        participation = (qty / bar.volume) if bar.volume > 0 else 0.0
        impact_bps = self.impact_coeff_bps * participation
        adverse = (self.slippage_bps + self.latency_bps + impact_bps) / 10_000.0
        fill_price = (ref_price * (1 + adverse) if work.direction is Direction.BUY
                      else ref_price * (1 - adverse))

        # Exchanges reject dust: enforce a minimum notional per fill.
        if self.min_notional > 0 and qty * fill_price < self.min_notional:
            log.warning("Order %.8f %s below min_notional $%.2f — rejected",
                        qty, work.symbol, self.min_notional)
            work.remaining = 0.0  # drop it; it can never clear at this size
            return None

        commission = qty * fill_price * self.commission_pct
        # Dollar slippage = qty * (adverse move vs the un-slipped reference).
        slippage_cost = qty * abs(fill_price - ref_price)
        work.remaining -= qty
        return FillEvent(
            dt=bar.dt, symbol=work.symbol, direction=work.direction,
            quantity=qty, fill_price=fill_price, commission=commission,
            slippage_cost=slippage_cost, exchange="SIM",
        )
