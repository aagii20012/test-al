"""Restart-equivalence property test (Decision 1: portfolio-authoritative sync).

The Generation 1 defect was that three strategies did not preserve required
position state across the per-cycle process restarts of the cloud "tick" bot, so
a restarted bot could make a *different* decision than an uninterrupted one. The
fix makes the portfolio the single source of truth: inside the shared event loop,
``strategy.sync_positions(portfolio)`` overwrites the strategy's position memory
from the book AFTER every portfolio-changing fill for the bar (fills, stops,
circuit-breaker flattens) and BEFORE the strategy reads that memory to decide.

The invariant, verbatim from the approved design:

    given identical candles/config/initial portfolio, three execution styles must
    produce identical decisions/fills/positions/realized P&L/final equity:
      (1) uninterrupted execution;
      (2) restart after every candle;
      (3) restart after every candle with strategy cache deliberately corrupted
          before synchronization.

We prove it by byte-comparing the full per-bar sequence of
``portfolio.dump_state()`` + ``risk.dump_state()`` (JSON-canonicalised) across the
three styles. That single artifact encodes positions, avg cost basis, realized
P&L, commissions, the equity curve, and every trade — so byte-equality is the
strongest possible statement of "identical".

A fourth "control" style (restart with sync DISABLED and the cache corrupted)
must DIVERGE — otherwise the equivalence above would be vacuous and would not
actually be exercising the sync that fixes the bug.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.engine.loop import dispatch_pending
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.research.grids import STRATEGY_REGISTRY
from algotrading.risk.risk_manager import RiskConfig, RiskManager

# Params chosen to actually trade (open, exit, and — for the signed strategies —
# reverse) over the scripted series below, so the equivalence is not vacuous.
PARAMS = {
    "momentum": {"lookback": 24, "threshold": 0.5},   # _pos, can short
    "bollinger": {"window": 20, "entry_z": 2.0, "exit_z": 0.5},  # _pos, can short
    "rsi": {"period": 14, "oversold": 30, "exit_level": 50},      # _in_market, long-only
}

PRIMARY = "BTCUSDT"
CAPITAL = 10_000.0


def _risk_config() -> RiskConfig:
    # ATR stops (fire on the scripted reversals -> forced exits -> the exact
    # desync the fix must heal) plus a daily-loss circuit breaker (halts+flattens
    # for the day, then resets next calendar day -> exercises persisted halt
    # state across restarts). Drawdown halt left off so it never permanently
    # ends trading mid-series.
    return RiskConfig(
        atr_sizing=True, atr_period=14, atr_stop_mult=2.5,
        risk_per_trade=0.02, max_position_pct=0.5, max_leverage=1.0,
        allow_short=True, use_stops=True,
        max_daily_loss_pct=0.05, max_drawdown_pct=0.0,
    )


def _scripted_frames() -> dict:
    """Deterministic multi-phase path: up, sharp crash, up, down.

    A small alternating jitter keeps per-bar volatility strictly positive (a
    perfectly smooth path has zero vol, which the vol-normalised momentum score
    treats as no-signal) and gives ATR a real value so stops can trigger.
    """
    phases = [(60, 0.012), (40, -0.025), (60, 0.015), (40, -0.018)]
    closes = []
    price = 100.0
    i = 0
    for length, drift in phases:
        for _ in range(length):
            jitter = 0.004 if i % 2 == 0 else -0.004
            price *= (1.0 + drift + jitter)
            closes.append(price)
            i += 1

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * 1.005 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.995 for o, c in zip(opens, closes)]
    idx = pd.DatetimeIndex(
        [datetime(2023, 1, 1) + timedelta(hours=k) for k in range(len(closes))])
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": [1_000_000.0] * len(closes)}, index=idx)
    return {PRIMARY: df}


def _n_bars(frames: dict) -> int:
    return len(next(iter(frames.values())))


def _canon(pf: Portfolio, risk: RiskManager) -> str:
    """Order-independent JSON of the full accounting + risk state for one bar."""
    return json.dumps(
        {"portfolio": pf.dump_state(), "risk": risk.dump_state()},
        sort_keys=True, default=str)


def _make(frames, strat_key):
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    risk = RiskManager(_risk_config())
    pf = Portfolio(data, events, risk, initial_capital=CAPITAL)
    execu = SimulatedExecutionHandler(
        events, data, commission_pct=0.001, slippage_bps=2.0,
        fill_at="close", min_notional=10.0)          # the production sim config
    strat = STRATEGY_REGISTRY[strat_key](data, events, **PARAMS[strat_key])
    return events, data, risk, pf, execu, strat


def _corrupt(strat) -> None:
    """Deliberately wreck the strategy's cached position memory.

    Claims "short everywhere / in-market everywhere" — the maximally stale state.
    Portfolio-authoritative sync must overwrite this from the book before the
    strategy ever reads it, so it cannot influence a single decision.
    """
    if hasattr(strat, "_pos"):
        strat._pos = {s: -1 for s in strat.symbols}
    if hasattr(strat, "_in_market"):
        strat._in_market = {s: True for s in strat.symbols}


def _run_uninterrupted(frames, strat_key):
    """Style 1: one long-lived process for the whole series."""
    events, data, risk, pf, execu, strat = _make(frames, strat_key)
    seq = []
    for _ in range(_n_bars(frames)):
        data.update_bars()                 # emits this bar's MarketEvent
        dispatch_pending(events, strat, pf, execu)
        seq.append(_canon(pf, risk))
    return seq


def _run_restart(frames, strat_key, *, corrupt=False, sync=True):
    """Styles 2/3/control: rebuild portfolio+risk+strategy from the JSON
    checkpoint every single bar, exactly like the cron 'tick' bot.

    The data handler is advanced in place (its trailing window is what a freshly
    launched tick reloads as `history`); the state-bearing objects — portfolio,
    risk, strategy, execution — are reconstructed and reloaded each bar, which is
    precisely what restart-equivalence is about. With fill_at="close" and full
    participation the executor carries no cross-bar state, so rebuilding it is a
    faithful no-op.
    """
    events = EventQueue()
    data = HistoricCSVDataHandler(events, frames)
    prev = None
    seq = []
    for _ in range(_n_bars(frames)):
        data.update_bars()                 # queue now holds exactly this bar's event

        risk = RiskManager(_risk_config())
        pf = Portfolio(data, events, risk, initial_capital=CAPITAL)
        execu = SimulatedExecutionHandler(
            events, data, commission_pct=0.001, slippage_bps=2.0,
            fill_at="close", min_notional=10.0)
        strat = STRATEGY_REGISTRY[strat_key](data, events, **PARAMS[strat_key])

        if prev is not None:
            pf.load_state(prev["portfolio"])
            risk.load_state(prev["risk"])
            strat.load_state(prev.get("strategy", {}))

        if not sync:
            strat.sync_positions = lambda portfolio: None   # simulate the bug
        if corrupt:
            _corrupt(strat)                # cache wrecked BEFORE sync runs in-loop

        dispatch_pending(events, strat, pf, execu)

        # Round-trip through JSON exactly like the on-disk checkpoint.
        prev = json.loads(json.dumps({
            "portfolio": pf.dump_state(),
            "risk": risk.dump_state(),
            "strategy": strat.dump_state(),
        }, default=str))
        seq.append(_canon(pf, risk))
    return seq


def _first_divergence(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None


@pytest.mark.parametrize("strat_key", ["momentum", "bollinger", "rsi"])
def test_restart_equivalence(strat_key):
    frames = _scripted_frames()

    base = _run_uninterrupted(frames, strat_key)
    restart = _run_restart(frames, strat_key)
    restart_corrupt = _run_restart(frames, strat_key, corrupt=True)

    assert len(base) == len(restart) == len(restart_corrupt) == _n_bars(frames)

    # The test must not be vacuous: the strategy has to actually trade.
    final = json.loads(base[-1])
    assert final["portfolio"]["trade_log"], (
        f"{strat_key} produced no trades on the scripted series")

    # (2) restart after every candle == (1) uninterrupted, bar for bar.
    i = _first_divergence(base, restart)
    assert i is None, (
        f"{strat_key}: restart diverged from uninterrupted at bar {i}\n"
        f"  uninterrupted: {base[i]}\n  restart:       {restart[i]}")

    # (3) restart with the strategy cache deliberately corrupted each bar ==
    #     uninterrupted: portfolio-authoritative sync erases the corruption.
    j = _first_divergence(base, restart_corrupt)
    assert j is None, (
        f"{strat_key}: corrupted-cache restart diverged at bar {j}\n"
        f"  uninterrupted:    {base[j]}\n  restart+corrupt:  {restart_corrupt[j]}")


def test_harness_is_discriminating_without_sync():
    """Guard against a vacuous pass: with sync DISABLED and the cache corrupted,
    a restarting bot MUST diverge from the uninterrupted run. This proves the
    scripted series is decision-sensitive and that sync is what makes the
    equivalence above hold (not some accident of the test setup)."""
    frames = _scripted_frames()
    base = _run_uninterrupted(frames, "momentum")
    buggy = _run_restart(frames, "momentum", corrupt=True, sync=False)

    assert _first_divergence(base, buggy) is not None, (
        "corrupted restart WITHOUT sync did not diverge — the equivalence test "
        "would be vacuous; strengthen the scripted series or corruption")
