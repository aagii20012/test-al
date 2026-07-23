"""Shared, network-free scaffolding for the Generation-2 test suite.

Everything here is deterministic and offline:
  * ``scripted_frames`` builds synthetic UTC-hourly OHLCV so ``ts.timestamp()``
    lands exactly on the hourly grid on every machine (a naive index would be
    interpreted in local time and could miss the grid).
  * ``GrowingFetch`` replays those frames through the ``fetch_ohlcv`` interface
    the coordinator/snapshot code consumes, with a mutable ``upto`` cursor so a
    test can advance the visible window one bar at a time.
  * ``write_config`` / ``build_test_manifest`` / ``make_coord`` assemble a fully
    self-contained experiment bound to a temp config (no repo config, no keys).

This module is NOT a test module (it does not match ``test_*.py``) so pytest
never collects it; test files import from it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml

from algotrading.gen2 import experiment as exp
from algotrading.gen2 import checkpoint as cp
from algotrading.gen2.experiment import Status, build_manifest
from algotrading.gen2.coordinator import Gen2Coordinator
from algotrading.gen2.snapshot import build_snapshot

# A fixed UTC anchor at HH:00:00 so every synthetic epoch is a multiple of 3600.
ANCHOR = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# A self-contained risk block mirroring config.ci.yaml's shape but with the
# knobs the equivalence/trading tests rely on made explicit. Circuit breakers
# that would silently gate trades are turned OFF except the daily-loss halt,
# which is deterministic and persisted, so it cannot break restart-equivalence.
DEFAULT_RISK = {
    "atr_sizing": True,
    "atr_period": 14,
    "atr_stop_mult": 2.5,
    "risk_per_trade": 0.02,
    "max_position_pct": 0.5,
    "max_leverage": 1.0,
    "allow_short": True,
    "use_stops": True,
    "stop_loss_pct": 0.05,
    "cash_buffer": 0.0,
    "max_daily_loss_pct": 0.0,
    "max_daily_profit_pct": 0.0,
    "max_drawdown_pct": 0.0,
}

# Params that make momentum actually trade on the scripted series below.
MOMENTUM_PARAMS = {"lookback": 24, "threshold": 0.5, "exit_band": 0.0}


def write_config(path, *, risk=None, financing_apr=0.10, initial_capital=10_000):
    """Write a minimal, self-contained YAML config load_config() understands."""
    cfg = {
        "exchange": "sim",
        "api_key": "",
        "api_secret": "",
        "testnet": True,
        "initial_capital": initial_capital,
        "cache_dir": "data_cache",
        "log_level": "WARNING",
        "financing_apr": financing_apr,
        "risk": dict(DEFAULT_RISK if risk is None else risk),
    }
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    return str(path)


def scripted_frames(symbols=("BTCUSDT",), *, phases=None, start=ANCHOR, seed_price=100.0):
    """Deterministic multi-phase OHLCV per symbol, UTC-hourly, oldest-first.

    ``phases`` is a list of ``(n_bars, drift)`` legs; a small alternating jitter
    is added so realised volatility is non-zero (momentum's vol-normalised score
    is undefined at zero vol) and ATR-based stops have something to bite on.
    Different symbols get slightly shifted seed prices so they are not identical.
    """
    phases = phases or [(105, 0.012), (60, -0.028), (40, 0.016)]
    frames = {}
    for si, symbol in enumerate(symbols):
        closes = []
        price = seed_price * (1.0 + 0.03 * si)
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
        vols = [1000.0 + (k % 7) * 10.0 for k in range(len(closes))]
        idx = pd.DatetimeIndex(
            [start + timedelta(hours=k) for k in range(len(closes))])
        frames[symbol] = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows,
             "close": closes, "volume": vols},
            index=idx)
    return frames


class GrowingFetch:
    """A ``fetch_ohlcv(symbol, interval, limit)`` replaying frozen frames.

    ``upto`` (None => all) caps the visible window to ``df.iloc[:upto + 1]`` so a
    test can walk boundaries forward one bar per tick, exactly as new closed
    candles would appear live.
    """

    def __init__(self, frames):
        self.frames = frames
        self.upto = None
        self.calls = []

    def __call__(self, symbol, interval="1h", limit=300):
        self.calls.append((symbol, interval, limit))
        df = self.frames[symbol]
        if self.upto is not None:
            df = df.iloc[: self.upto + 1]
        return df.tail(limit)


class DuplicateFetch:
    """Returns a frame whose last two candles share a timestamp (malformed)."""

    def __init__(self, frames):
        self.frames = frames

    def __call__(self, symbol, interval="1h", limit=300):
        df = self.frames[symbol].tail(limit).copy()
        idx = list(df.index)
        idx[-1] = idx[-2]                     # duplicate the penultimate epoch
        df.index = pd.DatetimeIndex(idx)
        return df


def build_test_manifest(config_path, *, symbols=None, strategies=None,
                        params_override=None, created=ANCHOR,
                        status=Status.PREPARED):
    """A manifest bound to ``config_path``, optionally restricted to a subset.

    Restricting bots/products changes the immutable payload, so the binding hash
    is recomputed — ``verify_binding()`` (called in the coordinator ctor) passes.
    """
    m = build_manifest(created=created, config_path=config_path, status=status)
    if symbols is None and strategies is None and not params_override:
        return m
    symbols = list(symbols) if symbols is not None else list(exp.SYMBOLS)
    strategies = list(strategies) if strategies is not None else list(exp.STRATEGIES)
    m.market = dict(m.market)
    m.market["products"] = {s: exp.PRODUCTS[s] for s in symbols}
    keep = []
    for b in m.bots:
        if b["strategy"] in strategies and b["symbol"] in symbols:
            b = dict(b)
            if params_override and b["strategy"] in params_override:
                b["params"] = dict(params_override[b["strategy"]])
                b["params_sha256"] = exp.sha256_canonical(b["params"])
            keep.append(b)
    m.bots = keep
    m.binding_sha256 = m.recompute_binding()
    return m


def make_coord(manifest, state_root, config_path, fetch, *,
               allow_fresh=True, verify_code=False):
    return Gen2Coordinator(
        manifest, state_root=str(state_root), config_path=str(config_path),
        fetch_ohlcv=fetch, allow_fresh=allow_fresh, verify_code=verify_code)


# --------------------------------------------------------------------------
# checkpoint-model helpers
#
# In the immutable-checkpoint model there is no "latest file" to stat: the ONLY
# published state is whatever ``CURRENT`` resolves to (with its manifest hash +
# every artifact hash verified). Tests therefore read published state exclusively
# through these helpers, never by globbing a directory.
# --------------------------------------------------------------------------
def make_snapshot(coord, fetch, *, upto=None):
    """Build the exact market snapshot ``coord`` would freeze for ``fetch``.

    Used to seed a hash-consistent CURRENT checkpoint (below) without running a
    full tick, and to drive a reference bot off byte-identical market data.
    """
    if upto is not None:
        fetch.upto = upto
    m = coord.manifest
    return build_snapshot(
        m.experiment_id, m.market["products"], fetch_ohlcv=fetch,
        interval=m.market["interval"], history=m.market["history"])


def current_ref(coord):
    """The parsed CURRENT pointer (or None); fail-closed on a corrupt pointer."""
    return coord.read_current()


def current_checkpoint(coord):
    """Resolve CURRENT to its fully hash-verified checkpoint (or None)."""
    return coord.resolve_current()


def bot_state(coord, bot_id):
    """The published on-disk state for one bot, read ONLY via CURRENT."""
    ckpt = coord.resolve_current()
    if ckpt is None:
        return None
    return ckpt.bot_states.get(bot_id)


def all_bot_states(coord):
    ckpt = coord.resolve_current()
    return dict(ckpt.bot_states) if ckpt is not None else {}


def checkpoint_names(coord):
    """Every materialised checkpoint dir (published OR orphan); ignores staging."""
    return cp.list_checkpoints(coord.exp_dir)


def seed_current_checkpoint(coord, bot_payloads, *, fetch, upto=None,
                            dry_run=True, prior=None, now=None):
    """Publish a hash-consistent CURRENT checkpoint from arbitrary bot payloads.

    The payloads may be intentionally corrupt / gen1 / unmarked / non-JSON: the
    checkpoint's own integrity (manifest + artifact hashes) is always consistent,
    so this exercises the per-bot schema/identity layer INDEPENDENTLY of the
    checkpoint-integrity layer. Returns the checkpoint name.
    """
    snap = make_snapshot(coord, fetch, upto=upto)
    return coord.seed_checkpoint(
        bot_payloads=bot_payloads, snapshot=snap, dry_run=dry_run,
        prior=prior, now=now or ANCHOR)


class CrashAt:
    """A ``coord._crash_hook`` that raises when a target publish stage fires.

    Stages (in publish order): ``after_stage_artifacts``,
    ``after_checkpoint_manifest``, ``after_fsync``, ``before_rename``,
    ``after_rename``, ``before_current``, ``during_current``, ``after_current``.
    ``.seen`` records every stage observed so a test can assert ordering.
    """

    def __init__(self, stage):
        self.stage = stage
        self.seen = []

    def __call__(self, stage):
        self.seen.append(stage)
        if stage == self.stage:
            raise RuntimeError(f"injected crash at stage {stage!r}")
