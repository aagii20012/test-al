"""Risk management layer.

Separated from the Portfolio on purpose:
  * Portfolio = accounting ("what do I hold, what is it worth?")
  * RiskManager = policy ("am I allowed to trade this, and how large?")

Responsibilities:
  1. Position sizing — fixed-fractional of equity, or ATR risk-parity (risk a
     fixed fraction of equity to a volatility-scaled stop), scaled by conviction.
  2. Hard limits: max position size %, max gross leverage, long/short gate.
  3. Per-trade exits: stop-loss, take-profit, and trailing stop — tracked as
     absolute price levels set at entry, so they work identically in backtest
     and live.
  4. Portfolio-level circuit breakers: maximum daily loss (halt for the rest of
     the day) and maximum drawdown (halt permanently); both flatten the book.
  5. Portfolio risk metric: historical Value-at-Risk (VaR).

The portfolio asks the risk manager to turn a SignalEvent into an OrderEvent;
the risk manager may also veto (return None), and on every bar it gets a chance
to emit forced-exit orders via `check_stops`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import numpy as np

from ..core.enums import Direction, OrderType, SignalType
from ..core.events import OrderEvent, SignalEvent
from ..utils.logger import get_logger

log = get_logger(__name__)

_UNSET = object()  # sentinel distinct from any real bar timestamp (incl. None)


@dataclass
class RiskConfig:
    # ---- sizing & hard limits -------------------------------------------
    max_position_pct: float = 0.20   # max fraction of equity in one symbol
    risk_per_trade: float = 0.10     # fixed-fractional: notional fraction / trade
                                     # ATR mode: fraction of equity risked to stop.
                                     # NOTE: this is the NOMINAL loss assuming the
                                     # stop fills AT its level. Real fills (gaps,
                                     # next-open latency, slippage) can realize a
                                     # larger loss; treat it as a target, not a
                                     # hard per-trade cap.
    max_leverage: float = 1.0        # gross exposure / equity ceiling
    allow_short: bool = True         # permit SHORT signals to open short positions
    cash_buffer: float = 0.0         # fraction of cash held back on buys (covers fees)

    # ---- per-trade exits ------------------------------------------------
    use_stops: bool = True
    stop_loss_pct: float = 0.05      # fixed stop distance (fraction of entry)
    take_profit_pct: float = 0.0     # 0 disables; else exit at +pct (R-relative)
    trailing_stop_pct: float = 0.0   # 0 disables; ratchets the stop behind price

    # ---- ATR (volatility) sizing & stops --------------------------------
    atr_sizing: bool = False         # size by risk-to-stop instead of notional
    atr_period: int = 14
    atr_stop_mult: float = 2.0       # stop distance = atr_stop_mult * ATR

    # ---- portfolio circuit breakers -------------------------------------
    max_daily_loss_pct: float = 0.0    # 0 disables; halt & flatten for the day
    max_daily_profit_pct: float = 0.0  # 0 disables; lock the win: halt & flatten
                                       # for the day once up this much (goal: bank
                                       # the day's gain and stop risking it)
    max_drawdown_pct: float = 0.0      # 0 disables; halt & flatten permanently


class RiskManager:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        # Per-symbol open-trade record: entry price, dir (+1/-1), stop, take, trail.
        self._open: Dict[str, dict] = {}
        # Per-bar accumulators for exposure/cash already COMMITTED this bar but
        # not yet filled. The event queue sizes every one of a bar's signals
        # before any of that bar's fills apply, so without these the leverage
        # and cash checks would each read the same stale (pre-bar) book and a
        # burst of same-bar entries could collectively breach the limits.
        self._pending_bar = _UNSET
        self._pending_gross = 0.0
        self._pending_cash = 0.0
        # Circuit-breaker state.
        self._peak_equity: Optional[float] = None
        self._day = None
        self._day_start_equity: Optional[float] = None
        self._halted_today = False
        self._halted_permanent = False

    # ---- helpers ---------------------------------------------------------
    def _atr(self, data, symbol: str) -> Optional[float]:
        period = self.config.atr_period
        bars = data.get_latest_bars(symbol, period + 1)
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, prev_close = bars[i].high, bars[i].low, bars[i - 1].close
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        atr = sum(trs[-period:]) / period
        return atr if atr > 0 else None

    @property
    def halted(self) -> bool:
        return self._halted_permanent or self._halted_today

    # ---- sizing ----------------------------------------------------------
    def size_order(
        self, signal: SignalEvent, portfolio, price: float
    ) -> Optional[OrderEvent]:
        if price <= 0:
            return None

        # New bar -> reset the committed-but-unfilled accumulators. Compare by
        # value: per-symbol bars at one timeline step are equal-valued but are
        # distinct datetime instances, so identity would reset within a bar.
        if self._pending_bar is _UNSET or signal.dt != self._pending_bar:
            self._pending_bar = signal.dt
            self._pending_gross = 0.0
            self._pending_cash = 0.0

        equity = portfolio.equity
        current_qty = portfolio.position(signal.symbol)

        if signal.signal_type is SignalType.EXIT:
            if current_qty == 0:
                return None
            direction = Direction.SELL if current_qty > 0 else Direction.BUY
            self._open.pop(signal.symbol, None)
            return OrderEvent(signal.symbol, OrderType.MARKET, abs(current_qty), direction)

        if signal.signal_type is SignalType.SHORT and not self.config.allow_short:
            return None

        # Circuit breakers veto all new entries (exits above are still allowed).
        if self.halted:
            return None

        target_dir = Direction.BUY if signal.signal_type is SignalType.LONG else Direction.SELL
        d = target_dir.sign  # +1 long, -1 short
        strength = float(signal.strength)

        # --- determine the stop distance (also drives ATR sizing) ----------
        atr = self._atr(portfolio.data, signal.symbol) if self.config.atr_sizing else None
        if self.config.atr_sizing and atr is not None:
            stop_dist = self.config.atr_stop_mult * atr
        else:
            stop_dist = price * self.config.stop_loss_pct

        # --- position size -------------------------------------------------
        if self.config.atr_sizing and stop_dist > 0:
            # Risk a fixed fraction of equity to the stop (risk parity).
            risk_dollars = equity * self.config.risk_per_trade * strength
            qty = risk_dollars / stop_dist
        else:
            dollar_budget = equity * self.config.risk_per_trade * strength
            qty = dollar_budget / price

        # Cap single-position exposure.
        max_qty = (equity * self.config.max_position_pct) / price
        qty = min(qty, max_qty)

        # Enforce gross leverage across the whole book, counting exposure already
        # committed (but not yet filled) earlier this bar.
        committed_gross = portfolio.gross_exposure + self._pending_gross
        if committed_gross + qty * price > equity * self.config.max_leverage:
            allowed = max(0.0, equity * self.config.max_leverage - committed_gross)
            qty = allowed / price
            log.warning("Leverage cap hit on %s; trimming order to %.6f", signal.symbol, qty)

        # A long cannot spend more cash than is actually available (minus any
        # cash already committed this bar and a buffer for fees). Shorts receive
        # cash, so this constraint applies to buys only.
        if target_dir is Direction.BUY:
            spendable = max(0.0, (portfolio.cash - self._pending_cash)
                            * (1.0 - self.config.cash_buffer))
            if qty * price > spendable:
                qty = spendable / price

        if qty <= 0:
            log.warning("Order for %s rejected by risk limits", signal.symbol)
            return None

        # Reserve this order's exposure/cash for the rest of the bar.
        self._pending_gross += qty * price
        if target_dir is Direction.BUY:
            self._pending_cash += qty * price

        # Record entry levels for stop / take-profit / trailing tracking.
        stop = price - d * stop_dist if (self.config.use_stops or self.config.trailing_stop_pct) else None
        take = price + d * price * self.config.take_profit_pct if self.config.take_profit_pct > 0 else None
        self._open[signal.symbol] = {
            "entry": price, "dir": d, "stop": stop, "take": take, "extreme": price,
        }
        return OrderEvent(signal.symbol, OrderType.MARKET, qty, target_dir)

    # ---- per-bar exit & circuit-breaker checks ---------------------------
    def check_stops(self, portfolio, data) -> List[OrderEvent]:
        orders: List[OrderEvent] = []

        # --- portfolio-level circuit breakers ------------------------------
        cb = self._circuit_breakers(portfolio, data)
        if cb:
            return cb  # flatten everything this bar; per-trade stops moot

        if not (self.config.use_stops or self.config.take_profit_pct
                or self.config.trailing_stop_pct):
            return orders

        for symbol, rec in list(self._open.items()):
            qty = portfolio.position(symbol)
            if qty == 0:
                self._open.pop(symbol, None)
                continue
            bar = data.get_latest_bar(symbol)
            if bar is None:
                continue
            price = bar.close
            d = rec["dir"]
            is_long = d > 0

            # Ratchet the trailing stop behind the favourable extreme.
            if self.config.trailing_stop_pct > 0:
                if is_long:
                    rec["extreme"] = max(rec["extreme"], price)
                    trail = rec["extreme"] * (1 - self.config.trailing_stop_pct)
                    rec["stop"] = max(rec["stop"], trail) if rec["stop"] else trail
                else:
                    rec["extreme"] = min(rec["extreme"], price)
                    trail = rec["extreme"] * (1 + self.config.trailing_stop_pct)
                    rec["stop"] = min(rec["stop"], trail) if rec["stop"] else trail

            hit_stop = (
                self.config.use_stops or self.config.trailing_stop_pct
            ) and rec["stop"] is not None and (
                (is_long and price <= rec["stop"]) or (not is_long and price >= rec["stop"])
            )
            hit_take = rec["take"] is not None and (
                (is_long and price >= rec["take"]) or (not is_long and price <= rec["take"])
            )

            if hit_stop or hit_take:
                reason = "TAKE-PROFIT" if hit_take and not hit_stop else "STOP"
                direction = Direction.SELL if is_long else Direction.BUY
                log.info("%s %s @ %.2f (entry %.2f)", reason, symbol, price, rec["entry"])
                orders.append(OrderEvent(symbol, OrderType.MARKET, abs(qty), direction))
                self._open.pop(symbol, None)
        return orders

    def _circuit_breakers(self, portfolio, data) -> List[OrderEvent]:
        """Daily-loss, daily-profit-lock, and max-drawdown switches.

        Returns flatten orders when a limit is hit. The daily-profit-lock is the
        mirror of the daily-loss halt: once the day is up enough, it banks the
        gain (flattens) and stops trading until the next day.
        """
        cfg = self.config
        if (cfg.max_daily_loss_pct <= 0 and cfg.max_drawdown_pct <= 0
                and cfg.max_daily_profit_pct <= 0):
            return []

        equity = portfolio.equity
        now = self._current_dt(data)

        # New calendar day -> reset daily baseline and the daily halt.
        if now is not None:
            day = now.date()
            if self._day != day:
                self._day = day
                self._day_start_equity = equity
                self._halted_today = False
        if self._day_start_equity is None:
            self._day_start_equity = equity
        self._peak_equity = equity if self._peak_equity is None else max(self._peak_equity, equity)

        breach_perm = (
            cfg.max_drawdown_pct > 0 and self._peak_equity > 0
            and (equity - self._peak_equity) / self._peak_equity <= -cfg.max_drawdown_pct
        )
        day_pnl_pct = ((equity - self._day_start_equity) / self._day_start_equity
                       if self._day_start_equity > 0 else 0.0)
        breach_day = cfg.max_daily_loss_pct > 0 and day_pnl_pct <= -cfg.max_daily_loss_pct
        breach_profit = cfg.max_daily_profit_pct > 0 and day_pnl_pct >= cfg.max_daily_profit_pct

        if breach_perm and not self._halted_permanent:
            self._halted_permanent = True
            log.warning("MAX-DRAWDOWN breached (%.2f%%) — halting permanently & flattening",
                        cfg.max_drawdown_pct * 100)
            return self._flatten(portfolio)
        if breach_day and not self._halted_today:
            self._halted_today = True
            log.warning("MAX-DAILY-LOSS breached (%.2f%%) — halting for the day & flattening",
                        cfg.max_daily_loss_pct * 100)
            return self._flatten(portfolio)
        if breach_profit and not self._halted_today:
            self._halted_today = True
            log.info("DAILY-PROFIT target hit (+%.2f%%) — locking the win & halting for the day",
                     cfg.max_daily_profit_pct * 100)
            return self._flatten(portfolio)
        return []

    def _flatten(self, portfolio) -> List[OrderEvent]:
        orders: List[OrderEvent] = []
        for symbol, qty in list(portfolio.positions.items()):
            if qty == 0:
                continue
            direction = Direction.SELL if qty > 0 else Direction.BUY
            orders.append(OrderEvent(symbol, OrderType.MARKET, abs(qty), direction))
            self._open.pop(symbol, None)
        return orders

    @staticmethod
    def _current_dt(data):
        latest = None
        for s in getattr(data, "symbols", []):
            bar = data.get_latest_bar(s)
            if bar is not None and (latest is None or bar.dt > latest):
                latest = bar.dt
        return latest

    def on_position_closed(self, symbol: str) -> None:
        self._open.pop(symbol, None)

    # ---- state persistence (for the run-once / cloud "tick" mode) --------
    def dump_state(self) -> dict:
        """Serialise circuit-breaker and open-trade state so halts, the daily
        baseline, and stop levels carry across separate runs (otherwise a cron
        bot would reset its daily-loss halt every hour)."""
        return {
            "open": {k: dict(v) for k, v in self._open.items()},
            "peak_equity": self._peak_equity,
            "day": self._day.isoformat() if self._day else None,
            "day_start_equity": self._day_start_equity,
            "halted_today": self._halted_today,
            "halted_permanent": self._halted_permanent,
        }

    def load_state(self, s: dict) -> None:
        self._open = {k: dict(v) for k, v in s.get("open", {}).items()}
        peak = s.get("peak_equity")
        self._peak_equity = float(peak) if peak is not None else None
        d = s.get("day")
        self._day = date.fromisoformat(d) if d else None
        dse = s.get("day_start_equity")
        self._day_start_equity = float(dse) if dse is not None else None
        self._halted_today = bool(s.get("halted_today", False))
        self._halted_permanent = bool(s.get("halted_permanent", False))

    # ---- portfolio risk metric ------------------------------------------
    @staticmethod
    def value_at_risk(returns: np.ndarray, confidence: float = 0.95) -> float:
        """Historical 1-period VaR as a positive fraction of equity.

        e.g. 0.03 at 95% means "on the worst 5% of periods, expect to lose >=3%".
        """
        if len(returns) < 2:
            return 0.0
        return float(-np.percentile(returns, (1 - confidence) * 100))
