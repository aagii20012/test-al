"""Portfolio: position & cash accounting plus the signal→order bridge.

Flow:
  * on SignalEvent  -> ask RiskManager to size/approve -> emit OrderEvent(s)
  * on FillEvent    -> update positions, cash, commissions
  * on MarketEvent  -> mark-to-market, append to equity curve, run stop checks

The portfolio is mode-agnostic: it works the same in backtest and live because
it only ever talks to the abstract DataHandler / EventQueue and the RiskManager.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List

import pandas as pd


def _parse_dt(value):
    """Round-trip helper: ISO string (from a saved state) back to a datetime."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value

from ..core.event_queue import EventQueue
from ..core.events import FillEvent, MarketEvent, SignalEvent
from ..core.enums import Direction
from ..data.base import DataHandler
from ..risk.risk_manager import RiskManager
from ..utils.logger import get_logger

log = get_logger(__name__)


class Portfolio:
    def __init__(
        self,
        data: DataHandler,
        events: EventQueue,
        risk: RiskManager,
        initial_capital: float = 100_000.0,
        *,
        financing_apr: float = 0.0,        # annual rate on borrowed/short notional
        periods_per_year: float = 365 * 24,  # bars per year (for per-bar accrual)
    ):
        self.data = data
        self.events = events
        self.risk = risk
        self.initial_capital = float(initial_capital)
        self.financing_apr = float(financing_apr)
        self.periods_per_year = float(periods_per_year)

        self.cash = float(initial_capital)
        self.positions: Dict[str, float] = defaultdict(float)        # qty per symbol
        self.avg_price: Dict[str, float] = defaultdict(float)        # cost basis
        self.realized_pnl = 0.0
        self.total_commission = 0.0
        self.total_slippage = 0.0    # $ slippage+latency drag (already in fills)
        self.total_financing = 0.0   # $ borrow/funding charged on held positions

        self.equity_curve: List[dict] = []
        self.trade_log: List[dict] = []

        # Append-only audit trail (Decision 2). `fills` holds ONE record per real
        # exchange fill, keyed by a monotonic `_fill_seq` id that survives
        # restarts; `legs` decomposes each fill into position-lifecycle legs
        # (OPEN / CLOSE) that all cite their parent fill_id. A reversal yields a
        # CLOSE leg AND an OPEN leg sharing one fill_id — it is still ONE fill,
        # so commission/slippage are recorded once on the fill, never on legs.
        self._fill_seq = 0
        self.fills: List[dict] = []
        self.legs: List[dict] = []

    # ---- queries used by the RiskManager ---------------------------------
    def position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def _price(self, symbol: str) -> float:
        bar = self.data.get_latest_bar(symbol)
        return bar.close if bar else self.avg_price.get(symbol, 0.0)

    @property
    def market_value(self) -> float:
        return sum(qty * self._price(s) for s, qty in self.positions.items())

    @property
    def gross_exposure(self) -> float:
        return sum(abs(qty) * self._price(s) for s, qty in self.positions.items())

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    # ---- event handlers --------------------------------------------------
    def update_signal(self, signal: SignalEvent) -> None:
        price = self._price(signal.symbol)
        order = self.risk.size_order(signal, self, price)
        if order is not None:
            self.events.put(order)

    def _accrue_financing(self) -> None:
        """Charge per-bar borrow/funding on short and leveraged exposure.

        Two non-overlapping bases, both realistic crypto carry costs:
          * short borrow  — you borrow the asset to short it: sum(|short notional|)
          * margin debit  — leveraged longs drive cash negative: max(0, -cash)
        Charged once per bar so it accrues for the whole holding period, not per
        trade. Zero by default and for spot, long-only, unlevered books.
        """
        if self.financing_apr <= 0:
            return
        short_notional = sum(max(0.0, -qty) * self._price(s)
                             for s, qty in self.positions.items())
        margin_debit = max(0.0, -self.cash)
        base = short_notional + margin_debit
        if base <= 0:
            return
        charge = base * self.financing_apr / self.periods_per_year
        self.cash -= charge
        self.total_financing += charge

    def update_timeindex(self, event: MarketEvent) -> None:
        """Accrue financing, mark-to-market, record equity, then run stops."""
        self._accrue_financing()
        self.equity_curve.append(
            {
                "dt": event.dt,
                "cash": self.cash,
                "market_value": self.market_value,
                "equity": self.equity,
            }
        )
        for order in self.risk.check_stops(self, self.data):
            self.events.put(order)

    def update_fill(self, fill: FillEvent) -> None:
        signed_qty = fill.quantity * fill.direction.sign
        prev_qty = self.positions[fill.symbol]
        new_qty = prev_qty + signed_qty

        # Cash impact: buys spend cash, sells receive cash; commission always paid.
        # Affordability is enforced upstream by RiskManager.size_order (a buy
        # cannot be sized beyond available cash, and gross leverage is capped),
        # so cash stays >= 0 for longs net of the optional fee buffer. Shorts
        # credit cash here without reserving margin — a standard spot-backtest
        # simplification; real margin/borrow is modelled at the live venue.
        self.cash -= signed_qty * fill.fill_price
        self.cash -= fill.commission
        self.total_commission += fill.commission
        # Slippage is already inside fill_price (do NOT debit cash again); track
        # it only for cost attribution / gross-vs-net reporting.
        self.total_slippage += getattr(fill, "slippage_cost", 0.0)

        # ---- append-only audit: decompose this ONE real fill into legs -------
        # A fill either opens/increases (one OPEN leg), reduces/closes (one CLOSE
        # leg), or reverses through zero (a CLOSE leg for the whole prior
        # position AND an OPEN leg for the residual) — every leg citing this one
        # fill_id. `prev_avg` is captured before the basis is updated below so the
        # CLOSE leg records the true entry price it is measured against.
        fill_id = self._fill_seq
        self._fill_seq += 1
        prev_avg = self.avg_price[fill.symbol]

        opposes = prev_qty != 0 and (prev_qty > 0) != (signed_qty > 0)
        closed = min(abs(signed_qty), abs(prev_qty)) if opposes else 0.0
        opened = abs(signed_qty) - closed

        # Realized P&L when reducing/closing a position (books ONLY the closed
        # quantity, at the pre-fill basis — unchanged accounting from Step 5).
        if closed > 0:
            direction_sign = 1 if prev_qty > 0 else -1
            pnl = (fill.fill_price - prev_avg) * closed * direction_sign
            self.realized_pnl += pnl
            self.trade_log.append(
                {
                    "dt": fill.dt, "symbol": fill.symbol, "side": fill.direction.value,
                    "qty": closed, "price": fill.fill_price, "realized_pnl": pnl,
                }
            )
            self.legs.append(
                {
                    "leg": "CLOSE", "fill_id": fill_id, "dt": fill.dt,
                    "symbol": fill.symbol, "side": fill.direction.value,
                    "qty": closed, "entry_price": prev_avg,
                    "exit_price": fill.fill_price, "realized_pnl": pnl,
                }
            )
        if opened > 0:
            # The opened lot's entry is THIS fill's price (a reversal residual or
            # a scale-in). The book's blended avg_price is tracked separately.
            self.legs.append(
                {
                    "leg": "OPEN", "fill_id": fill_id, "dt": fill.dt,
                    "symbol": fill.symbol, "side": fill.direction.value,
                    "qty": opened, "entry_price": fill.fill_price,
                    "exit_price": None, "realized_pnl": 0.0,
                }
            )

        # One record per real fill — commission/slippage counted here, ONCE.
        self.fills.append(
            {
                "fill_id": fill_id, "dt": fill.dt, "symbol": fill.symbol,
                "side": fill.direction.value, "qty": fill.quantity,
                "price": fill.fill_price, "commission": fill.commission,
                "slippage_cost": getattr(fill, "slippage_cost", 0.0),
                "exchange": getattr(fill, "exchange", "SIM"),
                "prev_qty": prev_qty, "new_qty": new_qty,
            }
        )

        # Update cost basis on increases / new positions.
        if new_qty == 0:
            self.avg_price[fill.symbol] = 0.0
            self.risk.on_position_closed(fill.symbol)
        elif (prev_qty >= 0 and signed_qty > 0) or (prev_qty <= 0 and signed_qty < 0):
            total_cost = self.avg_price[fill.symbol] * abs(prev_qty) + fill.gross_value
            self.avg_price[fill.symbol] = total_cost / abs(new_qty)
        elif prev_qty * new_qty < 0:
            # Reversal that crosses THROUGH zero (e.g. +5 sold 8 -> -3). This is
            # one real fill at one real price, not two fabricated exchange fills:
            # the closing leg's P&L was already booked above on close_quantity
            # (= min(|prev|, |fill|)), and the residual is a NEW opposite
            # position opened at THIS fill's price. Its cost basis is therefore
            # the actual fill price. Leaving the pre-reversal basis in place was
            # the Generation 1 defect: the next reduce/close mis-measured P&L
            # against a stale long/short entry that no longer existed.
            self.avg_price[fill.symbol] = fill.fill_price

        self.positions[fill.symbol] = new_qty
        log.info(
            "FILL %s %s %.6f @ %.2f | pos=%.6f cash=%.2f equity=%.2f",
            fill.direction.value, fill.symbol, fill.quantity, fill.fill_price,
            new_qty, self.cash, self.equity,
        )

    # ---- state persistence (for the run-once / cloud "tick" mode) --------
    # The checkpoint is committed every hour by the cloud bot, so the audit
    # lists are capped to a bounded tail: resuming only needs cash/positions/
    # cost-basis/totals, and an unbounded list would bloat the file and git
    # history forever. These tails still cover months of hourly history.
    _MAX_EQUITY_ROWS = 5000   # ~7 months at 1h
    _MAX_TRADE_ROWS = 1000
    _MAX_FILL_ROWS = 2000     # append-only fill ledger tail
    _MAX_LEG_ROWS = 4000      # up to ~2 legs per fill (reversals)

    def dump_state(self) -> dict:
        """Serialise everything needed to resume accounting on a later run."""
        def _ser(rows, cap):
            out = []
            for r in rows[-cap:]:
                r = dict(r)
                if hasattr(r.get("dt"), "isoformat"):
                    r["dt"] = r["dt"].isoformat()
                out.append(r)
            return out

        return {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": dict(self.positions),
            "avg_price": dict(self.avg_price),
            "realized_pnl": self.realized_pnl,
            "total_commission": self.total_commission,
            "total_slippage": self.total_slippage,
            "total_financing": self.total_financing,
            "equity_curve": _ser(self.equity_curve, self._MAX_EQUITY_ROWS),
            "trade_log": _ser(self.trade_log, self._MAX_TRADE_ROWS),
            "fill_seq": self._fill_seq,
            "fills": _ser(self.fills, self._MAX_FILL_ROWS),
            "legs": _ser(self.legs, self._MAX_LEG_ROWS),
        }

    def load_state(self, s: dict) -> None:
        self.initial_capital = float(s.get("initial_capital", self.initial_capital))
        self.cash = float(s["cash"])
        self.positions = defaultdict(float, {k: float(v) for k, v in s.get("positions", {}).items()})
        self.avg_price = defaultdict(float, {k: float(v) for k, v in s.get("avg_price", {}).items()})
        self.realized_pnl = float(s.get("realized_pnl", 0.0))
        self.total_commission = float(s.get("total_commission", 0.0))
        self.total_slippage = float(s.get("total_slippage", 0.0))
        self.total_financing = float(s.get("total_financing", 0.0))
        self.equity_curve = [{**e, "dt": _parse_dt(e["dt"])} for e in s.get("equity_curve", [])]
        self.trade_log = [{**t, "dt": _parse_dt(t["dt"])} for t in s.get("trade_log", [])]
        # `fill_seq` must resume from the saved high-water mark so fill ids stay
        # monotonic across restarts even after the fills tail has been capped.
        self._fill_seq = int(s.get("fill_seq", len(s.get("fills", []))))
        self.fills = [{**f, "dt": _parse_dt(f["dt"])} for f in s.get("fills", [])]
        self.legs = [{**l, "dt": _parse_dt(l["dt"])} for l in s.get("legs", [])]

    # ---- output ----------------------------------------------------------
    def equity_dataframe(self) -> pd.DataFrame:
        if not self.equity_curve:
            return pd.DataFrame(columns=["equity"])
        df = pd.DataFrame(self.equity_curve).set_index("dt")
        df["returns"] = df["equity"].pct_change().fillna(0.0)
        return df
