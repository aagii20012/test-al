"""Restart-equivalence: ticking-from-disk must equal never-restarting.

The coordinator rebuilds every bot from its on-disk checkpoint on *every* tick
(load_state -> run one bar -> dump_state). A cloud cron that fires hourly is
therefore a fresh process each hour. This must produce the *byte-identical*
result of a hypothetical process that stayed alive the whole time and never
serialised anything — otherwise the hourly checkpoint is silently lossy and the
published equity is a fiction.

We prove it directly:

  * REFERENCE (ground truth) — one persistent Portfolio / RiskManager / Strategy
    built once, fed a fresh warmed-up data handler each boundary (``.data`` /
    ``.events`` re-pointed), NEVER dumped or loaded. This is the "process that
    never died".

  * COORDINATOR (under test) — the real per-tick reload path, driven off its OWN
    published immutable snapshot so both sides read identical market data.

At every decision boundary the coordinator's on-disk portfolio / risk / strategy
sub-state must equal the reference's in-memory dump byte-for-byte (compared via
the same canonical JSON the manifest binding uses). A single non-round-tripping
field anywhere would surface as a divergence at some boundary.

A second test corrupts the persisted strategy cache on every load and shows the
result is unchanged: ``sync_positions`` overwrites ``_pos`` from the book (the
portfolio is authoritative) before the strategy reads it, so a poisoned cache
cannot influence a decision. All offline; dry-run only; nothing is activated.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, MOMENTUM_PARAMS, GrowingFetch, build_test_manifest, make_coord,
    scripted_frames, write_config)

from algotrading.core.event_queue import EventQueue  # noqa: E402
from algotrading.core.events import MarketEvent  # noqa: E402
from algotrading.data.live import LiveDataHandler  # noqa: E402
from algotrading.engine.loop import dispatch_pending  # noqa: E402
from algotrading.execution.simulated import SimulatedExecutionHandler  # noqa: E402
from algotrading.gen2 import experiment as exp  # noqa: E402
from algotrading.gen2.coordinator import _PERIODS_PER_YEAR_1H  # noqa: E402
from algotrading.gen2.snapshot import SnapshotError, SnapshotExchange  # noqa: E402
from algotrading.portfolio.portfolio import Portfolio  # noqa: E402
from algotrading.research.grids import STRATEGY_REGISTRY  # noqa: E402
from algotrading.risk.risk_manager import RiskManager  # noqa: E402
from algotrading.strategy.momentum import MomentumStrategy  # noqa: E402

SUB_STATES = ("portfolio", "risk", "strategy")


class _ContinuousBot:
    """The process that never restarts: persistent book, re-pointed data feed.

    Mirrors the coordinator's ``_run_bot`` decision path exactly (LiveDataHandler
    warm-up -> one injected MarketEvent -> dispatch_pending with a fresh
    SimulatedExecutionHandler), but keeps the Portfolio / RiskManager / Strategy
    alive across ticks instead of dumping + reloading. RiskManager takes
    portfolio/data as call args, so only the portfolio's and strategy's
    ``.data`` / ``.events`` need re-pointing each tick.
    """

    def __init__(self, coord, bot_def):
        self.coord = coord
        self.bot_def = bot_def
        self.symbol = bot_def["symbol"]
        self._risk_cfg = coord._risk_config()
        self._financing = coord._financing_apr()
        self.risk = RiskManager(self._risk_cfg)
        self.portfolio = None
        self.strategy = None

    def tick(self, snapshot, decision_epoch_ms):
        m = self.coord.manifest
        events = EventQueue()
        exchange = SnapshotExchange(snapshot)
        data = LiveDataHandler(events, exchange, [self.symbol],
                               interval=snapshot.interval,
                               history=m.market["history"], drop_forming=False)
        if self.portfolio is None:
            self.portfolio = Portfolio(
                data, events, self.risk,
                initial_capital=float(self.bot_def["initial_capital"]),
                financing_apr=self._financing,
                periods_per_year=_PERIODS_PER_YEAR_1H)
            self.strategy = STRATEGY_REGISTRY[self.bot_def["strategy"]](
                data, events, **self.bot_def["params"])
        else:
            # Re-point the live feed; the book itself is never re-created.
            self.portfolio.data = data
            self.portfolio.events = events
            self.strategy.data = data
            self.strategy.events = events

        latest = data.get_latest_bar(self.symbol)
        assert latest is not None
        assert int(latest.dt.timestamp() * 1000) == decision_epoch_ms
        events.put(MarketEvent(dt=latest.dt))
        cm = m.cost_model
        dispatch_pending(
            events, self.strategy, self.portfolio,
            execution=SimulatedExecutionHandler(
                events, data, commission_pct=cm["commission_pct"],
                slippage_bps=cm["slippage_bps"], fill_at=cm["fill_at"],
                min_notional=cm["min_notional"]))

    def dump(self):
        return {
            "portfolio": self.portfolio.dump_state(),
            "risk": self.risk.dump_state(),
            "strategy": self.strategy.dump_state(),
        }


def _single_momentum_coord(tmp_path, subdir):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))                 # 205 hourly bars
    fetch = GrowingFetch(frames)
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / subdir, cfg, fetch)
    coord.prepare()
    return coord, fetch, frames


# --------------------------------------------------------------------------
# the core proof
# --------------------------------------------------------------------------
def test_restart_reload_equals_never_restarting(tmp_path):
    coord, fetch, frames = _single_momentum_coord(tmp_path, "state")
    bot_def = coord.manifest.bots[0]
    bot_id = bot_def["bot_id"]
    ref = _ContinuousBot(coord, bot_def)

    n = len(frames["BTCUSDT"])
    compared = 0
    for i in range(n):
        fetch.upto = i
        try:
            r = coord.run_tick(dry_run=True, now=ANCHOR + timedelta(hours=i))
        except SnapshotError as e:
            # Early bars are below MIN_WARMUP; neither side advances -> lockstep.
            assert e.kind == "insufficient_warmup"
            continue
        assert r.status == "PUBLISHED", (i, r.status)
        epoch = r.decision_epoch_ms

        # Drive the reference off the coordinator's OWN published snapshot so both
        # sides read byte-identical market data. The just-published checkpoint
        # (named by CURRENT, resolved with full hash-verification) carries both the
        # frozen market snapshot and the bot's on-disk sub-state for this boundary.
        ck = coord.resolve_current()
        assert ck is not None and ck.boundary_epoch == epoch
        ref.tick(ck.snapshot, epoch)

        disk = ck.bot_states[bot_id]
        want = ref.dump()
        for sub in SUB_STATES:
            assert exp.canonical_json(disk[sub]) == exp.canonical_json(want[sub]), (
                f"boundary {i} ({epoch}): {sub} sub-state diverged between the "
                "per-tick-reload path and the never-restart reference")
        compared += 1

    # Non-vacuous: the default series yields ~96 decision boundaries, and the
    # bot actually trades (long -> short reversals) across them.
    assert compared >= 90, f"only {compared} boundaries compared"


# --------------------------------------------------------------------------
# a poisoned strategy cache cannot change the outcome
# --------------------------------------------------------------------------
def test_corrupted_strategy_cache_heals(tmp_path, monkeypatch):
    def run(subdir):
        coord, fetch, frames = _single_momentum_coord(tmp_path, subdir)
        bot_id = coord.manifest.bots[0]["bot_id"]
        states = []
        n = len(frames["BTCUSDT"])
        for i in range(n):
            fetch.upto = i
            try:
                coord.run_tick(dry_run=True, now=ANCHOR + timedelta(hours=i))
            except SnapshotError as e:
                assert e.kind == "insufficient_warmup"
                continue
            disk = coord.resolve_current().bot_states[bot_id]
            states.append({k: disk[k] for k in ("portfolio", "strategy")})
        return states

    clean = run("clean")

    # Poison _pos on every load. sync_positions must overwrite it from the book
    # before calculate_signals reads it, so both signals AND the dumped state are
    # unchanged. (The first boundary has no prior state -> load_state isn't even
    # called there; corruption is exercised on every subsequent boundary.)
    real_load = MomentumStrategy.load_state

    def corrupt_load(self, state):
        real_load(self, state)
        self._pos = {s: -7 for s in self.symbols}     # garbage sign + magnitude

    monkeypatch.setattr(MomentumStrategy, "load_state", corrupt_load)
    healed = run("healed")

    assert len(clean) == len(healed) >= 90
    for i, (a, b) in enumerate(zip(clean, healed)):
        assert exp.canonical_json(a["portfolio"]) == exp.canonical_json(b["portfolio"]), (
            f"boundary idx {i}: portfolio differs under corrupted cache")
        assert exp.canonical_json(a["strategy"]) == exp.canonical_json(b["strategy"]), (
            f"boundary idx {i}: strategy differs under corrupted cache")
