"""Generation-2 coordinator behaviour: the safety-critical invariants.

Every test is offline (a synthetic fetch is injected) and writes only under a
pytest ``tmp_path`` state root. Nothing here activates trading for real, touches
a Generation-1 file, or reaches the network.

In the immutable-checkpoint model the ONLY published state is whatever ``CURRENT``
resolves to, with its checkpoint-manifest hash + every artifact hash verified.
Tests therefore read published state exclusively through the checkpoint helpers
(``current_ref`` / ``current_checkpoint`` / ``bot_state`` / ``checkpoint_names``),
never by globbing a directory for a "latest" file.

Covered here:
  * all-8 initialisation                         (test_all_eight_init_*)
  * one shared decision boundary + one snapshot  (test_common_boundary_single_snapshot)
  * partial failure -> ZERO publication          (test_partial_failure_publishes_nothing)
  * idempotency (re-run never re-advances)        (test_idempotent_rerun_*)
  * overlapping-run lock + stale steal            (test_overlapping_run_*)
  * corrupted / schema-mismatched prior state     (test_corrupted_prior_*, test_schema_mismatch_*)
  * atomic publication, no leftovers              (test_publish_atomic_no_leftovers)
  * PREPARED vs ACTIVE gating                     (test_prepared_vs_active)
  * accounting reconciliation                     (test_accounting_reconciliation_*)
  * code-drift fail-closed                        (test_code_drift_rejected)
  * namespace isolation / Gen1 byte-identical     (test_namespace_isolation_*)

Crash-injection, checkpoint-integrity, orphan-recovery and market-data
provenance rejections live in dedicated files
(``test_gen2_checkpoint_crash.py`` and ``test_gen2_provenance.py``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, MOMENTUM_PARAMS, GrowingFetch, all_bot_states, bot_state,
    build_test_manifest, checkpoint_names, current_checkpoint, current_ref,
    make_coord, scripted_frames, seed_current_checkpoint, write_config)

from datetime import timedelta  # noqa: E402

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import checkpoint as cp  # noqa: E402
from algotrading.gen2 import experiment as exp  # noqa: E402
from algotrading.gen2.experiment import Status  # noqa: E402
from algotrading.gen2.coordinator import (  # noqa: E402
    BotResult, Gen2Coordinator, NotActivatedError, OverlappingRunError,
    TickAborted, _RECON_TOL)


# --------------------------------------------------------------------------
# small builders
# --------------------------------------------------------------------------
def _one_bot(tmp_path, *, frames=None, upto=None, **cfg_kw):
    cfg = write_config(tmp_path / "cfg.yaml", **cfg_kw)
    frames = frames or scripted_frames(("BTCUSDT",))
    fetch = GrowingFetch(frames)
    fetch.upto = upto
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / "state", cfg, fetch)
    return coord, fetch, m


def _staging_dirs(coord):
    root = coord.checkpoints_dir
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if n.startswith(cp.STAGING_PREFIX))


def _tmp_leftovers(coord):
    out = []
    for root, _dirs, files in os.walk(coord.exp_dir):
        out += [os.path.join(root, f) for f in files if f.endswith(".tmp")]
    return out


# --------------------------------------------------------------------------
# all-8 init
# --------------------------------------------------------------------------
def test_all_eight_init_publishes_all_bots(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT", "ETHUSDT"))
    coord = make_coord(build_test_manifest(cfg), tmp_path / "state", cfg,
                       GrowingFetch(frames))
    coord.prepare()

    res = coord.run_tick(dry_run=True, now=ANCHOR)

    assert res.status == "PUBLISHED"
    assert len(res.bots) == 8
    # the checkpoint is content-addressed: <boundary_epoch>-<snapshot_sha256>
    assert res.checkpoint == cp.checkpoint_name(res.decision_epoch_ms,
                                                res.snapshot_sha256)
    assert os.path.exists(coord.current_path)
    # exactly ONE checkpoint materialised (no orphans on a clean run)
    assert checkpoint_names(coord) == [res.checkpoint]

    ref = current_ref(coord)
    assert ref is not None
    assert ref.checkpoint == res.checkpoint
    assert ref.checkpoint_manifest_sha256 == res.checkpoint_manifest_sha256
    assert ref.prior_checkpoint is None
    assert ref.boundary_epoch == res.decision_epoch_ms

    # CURRENT resolves to a fully hash-verified checkpoint holding all 8 bots.
    states = all_bot_states(coord)
    assert set(states) == set(coord.manifest.bot_ids())
    assert len(states) == 8

    for row in res.bots:
        assert row["acted"] is True
        assert row["last_bar_ts"] == res.decision_epoch_ms

    for bid in coord.manifest.bot_ids():
        st = bot_state(coord, bid)
        assert st["experiment_id"] == coord.manifest.experiment_id
        assert st["bot_id"] == bid
        assert st["last_bar_ts"] == res.decision_epoch_ms
        assert st["portfolio"]["initial_capital"] == 10_000.0
        assert state_schema.is_current(st)


# --------------------------------------------------------------------------
# one shared boundary + exactly one snapshot
# --------------------------------------------------------------------------
def test_common_boundary_single_snapshot(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    # ETH is 5 bars shorter -> the shared boundary is ETH's last closed hour.
    btc = scripted_frames(("BTCUSDT",), phases=[(105, 0.012), (45, -0.02)])   # 150
    eth = scripted_frames(("ETHUSDT",), phases=[(105, 0.012), (40, -0.02)])   # 145
    frames = {**btc, **eth}
    m = build_test_manifest(cfg, symbols=("BTCUSDT", "ETHUSDT"),
                            strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / "state", cfg, GrowingFetch(frames))
    coord.prepare()

    res = coord.run_tick(dry_run=True, now=ANCHOR)

    # exactly ONE published checkpoint, holding exactly ONE shared snapshot.
    assert checkpoint_names(coord) == [res.checkpoint]
    ckpt = current_checkpoint(coord)
    assert os.path.isfile(os.path.join(ckpt.dir, cp.SNAPSHOT_NAME))
    snap = ckpt.snapshot

    expected_epoch = int((ANCHOR + timedelta(hours=144)).timestamp() * 1000)
    assert res.decision_epoch_ms == expected_epoch
    assert snap.shared_candle_epoch_ms == expected_epoch
    assert set(snap.symbols) == {"BTCUSDT", "ETHUSDT"}
    # the BTC window was sliced down to the shared boundary (its last candle ts)
    assert snap.candles["BTCUSDT"][-1][0] == expected_epoch
    assert snap.candles["ETHUSDT"][-1][0] == expected_epoch
    for row in res.bots:
        assert row["last_bar_ts"] == expected_epoch


# --------------------------------------------------------------------------
# partial failure -> zero publication
# --------------------------------------------------------------------------
def test_partial_failure_publishes_nothing(tmp_path, monkeypatch):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT", "ETHUSDT"))
    m = build_test_manifest(cfg, symbols=("BTCUSDT", "ETHUSDT"),
                            strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / "state", cfg, GrowingFetch(frames))
    coord.prepare()

    real_run_bot = Gen2Coordinator._run_bot

    def flaky(self, bot_def, snapshot, epoch, prev):
        if bot_def["bot_id"] == "momentum_ETHUSDT":
            raise TickAborted("injected bot failure", bot_def["bot_id"])
        return real_run_bot(self, bot_def, snapshot, epoch, prev)

    monkeypatch.setattr(Gen2Coordinator, "_run_bot", flaky)

    with pytest.raises(TickAborted):
        coord.run_tick(dry_run=True, now=ANCHOR)

    # The abort is in COMPUTE, before any checkpoint dir is even staged: NOTHING
    # materialised. No checkpoint, no orphan, no staging, no CURRENT, no lock.
    assert checkpoint_names(coord) == []
    assert _staging_dirs(coord) == []
    assert current_ref(coord) is None
    assert not os.path.exists(coord.current_path)
    assert not os.path.exists(coord.lock_path)
    assert _tmp_leftovers(coord) == []


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------
def test_idempotent_rerun_returns_already_published(tmp_path):
    coord, fetch, _ = _one_bot(tmp_path, upto=160)
    coord.prepare()

    r1 = coord.run_tick(dry_run=True, now=ANCHOR)
    assert r1.status == "PUBLISHED"

    current_before = open(coord.current_path, encoding="utf-8").read()
    names_before = checkpoint_names(coord)
    state_before = bot_state(coord, "momentum_BTCUSDT")

    # same visible window -> same boundary -> idempotent no-op, no writes.
    r2 = coord.run_tick(dry_run=True, now=ANCHOR + timedelta(hours=1))
    assert r2.status == "ALREADY_PUBLISHED"
    assert r2.decision_epoch_ms == r1.decision_epoch_ms
    assert r2.checkpoint == r1.checkpoint
    # CURRENT byte-identical, no new checkpoint, bot state untouched.
    assert open(coord.current_path, encoding="utf-8").read() == current_before
    assert checkpoint_names(coord) == names_before
    assert bot_state(coord, "momentum_BTCUSDT") == state_before


def test_idempotency_key_is_experiment_plus_epoch(tmp_path):
    coord, _, m = _one_bot(tmp_path, upto=160)
    coord.prepare()
    r = coord.run_tick(dry_run=True, now=ANCHOR)
    assert r.idempotency_key == f"{m.experiment_id}:{r.decision_epoch_ms}"


# --------------------------------------------------------------------------
# overlapping-run protection
# --------------------------------------------------------------------------
def test_overlapping_run_rejected_then_stale_lock_stolen(tmp_path):
    coord, _, _ = _one_bot(tmp_path, upto=160)
    coord.prepare()

    with open(coord.lock_path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 999999, "acquired_utc": ANCHOR.isoformat()}, fh)

    # A fresh lock (age 10s) blocks a concurrent tick.
    with pytest.raises(OverlappingRunError):
        coord.run_tick(dry_run=True, now=ANCHOR + timedelta(seconds=10))
    assert os.path.exists(coord.lock_path)   # not stolen
    assert current_ref(coord) is None        # nothing published under contention

    # The same lock, now older than the TTL (>900s), is stolen and the tick runs.
    r = coord.run_tick(dry_run=True, now=ANCHOR + timedelta(seconds=1000))
    assert r.status == "PUBLISHED"
    assert not os.path.exists(coord.lock_path)   # released on exit
    assert current_ref(coord).checkpoint == r.checkpoint


# --------------------------------------------------------------------------
# corrupted / schema-mismatched prior checkpoint state
#
# Each test seeds a hash-consistent CURRENT checkpoint whose bot payload is
# intentionally bad, then runs a tick at a LATER boundary. The checkpoint's own
# integrity is fine (its bytes hash to its manifest), so this exercises the
# per-bot schema/identity layer INDEPENDENTLY of the checkpoint-integrity layer.
# Every case must fail closed and leave CURRENT pinned at the seeded checkpoint.
# --------------------------------------------------------------------------
def test_corrupted_prior_state_fails_closed(tmp_path):
    coord, fetch, _ = _one_bot(tmp_path)
    coord.prepare()
    seeded = seed_current_checkpoint(
        coord, {"momentum_BTCUSDT": "{ this is not valid json ]"},
        fetch=fetch, upto=150)

    fetch.upto = 160
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.run_tick(dry_run=True, now=ANCHOR)

    # Fail-closed: CURRENT still resolves to the seeded checkpoint, and no new
    # (higher-boundary) checkpoint was published.
    assert current_ref(coord).checkpoint == seeded
    assert checkpoint_names(coord) == [seeded]


def test_schema_mismatch_state_rejected(tmp_path):
    coord, fetch, m = _one_bot(tmp_path)
    coord.prepare()
    # valid JSON, but a Generation-1 marker -> fail closed, no migration.
    legacy = {
        "generation": "gen1", "schema_version": 1,
        "experiment_id": m.experiment_id, "bot_id": "momentum_BTCUSDT",
        "last_bar_ts": 0, "portfolio": {}, "risk": {}, "strategy": {},
    }
    seeded = seed_current_checkpoint(
        coord, {"momentum_BTCUSDT": legacy}, fetch=fetch, upto=150)

    fetch.upto = 160
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.run_tick(dry_run=True, now=ANCHOR)
    assert current_ref(coord).checkpoint == seeded
    assert checkpoint_names(coord) == [seeded]


def test_unmarked_state_rejected(tmp_path):
    coord, fetch, _ = _one_bot(tmp_path)
    coord.prepare()
    seeded = seed_current_checkpoint(
        coord, {"momentum_BTCUSDT": {"portfolio": {}, "cash": 1.0}},  # no marker
        fetch=fetch, upto=150)

    fetch.upto = 160
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.run_tick(dry_run=True, now=ANCHOR)
    assert current_ref(coord).checkpoint == seeded


def test_roster_checkpoint_mismatch_rejected(tmp_path):
    """A published checkpoint missing a roster bot fails closed (no partial run)."""
    coord, fetch, m = _one_bot(tmp_path)
    coord.prepare()
    # Seed a checkpoint that carries the WRONG bot id (roster expects momentum).
    good = {
        "generation": state_schema.GENERATION,
        "schema_version": state_schema.SCHEMA_VERSION,
        "experiment_id": m.experiment_id, "bot_id": "rsi_BTCUSDT",
        "last_bar_ts": 0, "portfolio": {}, "risk": {}, "strategy": {},
    }
    seeded = seed_current_checkpoint(
        coord, {"rsi_BTCUSDT": good}, fetch=fetch, upto=150)

    fetch.upto = 160
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.run_tick(dry_run=True, now=ANCHOR)
    assert current_ref(coord).checkpoint == seeded


# --------------------------------------------------------------------------
# atomic publication (clean run leaves no staging / tmp litter)
# --------------------------------------------------------------------------
def test_publish_atomic_no_leftovers(tmp_path):
    coord, _, _ = _one_bot(tmp_path, upto=160)
    coord.prepare()

    r = coord.run_tick(dry_run=True, now=ANCHOR)
    assert r.status == "PUBLISHED"

    # CURRENT resolves to exactly the published, fully-verified checkpoint.
    ckpt = current_checkpoint(coord)
    assert ckpt.name == r.checkpoint
    assert ckpt.checkpoint_manifest_sha256 == r.checkpoint_manifest_sha256

    # No staging dirs and no *.tmp litter anywhere under the experiment dir.
    assert _staging_dirs(coord) == []
    assert _tmp_leftovers(coord) == []

    # Tampering the CHECKPOINT_MANIFEST is caught on the next resolve (fail closed).
    with open(os.path.join(ckpt.dir, cp.CHECKPOINT_MANIFEST_NAME),
              "a", encoding="utf-8") as fh:
        fh.write("\n")   # one byte changes the manifest hash
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.resolve_current()


# --------------------------------------------------------------------------
# PREPARED vs ACTIVE
# --------------------------------------------------------------------------
def test_prepared_vs_active(tmp_path):
    coord, _, _ = _one_bot(tmp_path, upto=160)
    coord.prepare()

    # PREPARED: a live tick is refused; only dry-runs are allowed.
    with pytest.raises(NotActivatedError):
        coord.run_tick(dry_run=False, now=ANCHOR)

    # Activation is human-gated: approved=True is mandatory.
    with pytest.raises(NotActivatedError):
        coord.set_status(Status.ACTIVE)

    coord.set_status(Status.ACTIVE, approved=True)
    r = coord.run_tick(dry_run=False, now=ANCHOR)
    assert r.status == "PUBLISHED"
    assert r.dry_run is False

    m2 = coord.load_manifest_from_disk()
    assert m2.status == "ACTIVE"
    assert any(h["to"] == "ACTIVE" and h["approved"] for h in m2.history)
    # activation is recorded as a NON-binding commit marker distinct from the
    # (binding) implementation commit / source-tree hash.
    assert "activation_commit" in m2.to_dict()


# --------------------------------------------------------------------------
# accounting reconciliation
# --------------------------------------------------------------------------
def test_accounting_reconciliation_holds_each_tick(tmp_path):
    coord, fetch, _ = _one_bot(tmp_path)
    coord.prepare()
    frames_len = len(fetch.frames["BTCUSDT"])

    residuals = []
    for i in range(120, frames_len, 8):     # a handful of ticks across the path
        fetch.upto = i
        r = coord.run_tick(dry_run=True, now=ANCHOR + timedelta(hours=i))
        for row in r.bots:
            residuals.append(abs(row["recon_residual"]))
    assert residuals                          # non-vacuous
    assert max(residuals) <= _RECON_TOL


def test_validate_result_rejects_bad_accounting():
    coord = Gen2Coordinator.__new__(Gen2Coordinator)   # no I/O needed

    def _mk(**kw):
        base = dict(bot_id="b", strategy="momentum", symbol="BTCUSDT",
                    acted=True, last_bar_ts=0, equity=10_000.0, cash=10_000.0,
                    position=0.0, realized_pnl=0.0, total_commission=0.0,
                    last_price=100.0, recon_residual=0.0)
        base.update(kw)
        return BotResult(**base)

    coord._validate_result(_mk())                       # clean -> ok
    with pytest.raises(TickAborted):
        coord._validate_result(_mk(recon_residual=1.0))
    with pytest.raises(TickAborted):
        coord._validate_result(_mk(equity=float("nan")))


# --------------------------------------------------------------------------
# code-drift fail-closed
# --------------------------------------------------------------------------
def test_code_drift_rejected(tmp_path, monkeypatch):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    fetch = GrowingFetch(frames)
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / "state", cfg, fetch, verify_code=True)
    coord.prepare()

    # Pretend the running source tree changed after the experiment was bound.
    monkeypatch.setattr(exp, "source_tree_hash",
                        lambda *a, **k: {"sha256": "de" * 32, "file_count": 1})
    with pytest.raises(state_schema.IncompatibleStateError):
        coord.run_tick(dry_run=True, now=ANCHOR)
    # nothing published under drift
    assert current_ref(coord) is None


# --------------------------------------------------------------------------
# namespace isolation / Gen1 byte-identical
# --------------------------------------------------------------------------
def test_namespace_isolation_gen1_byte_identical(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    # Representative frozen Generation-1 files that must never be touched.
    gen1 = {}
    for name in ("momentum_BTCUSDT_sim.json", "rsi_ETHUSDT_sim.json"):
        p = state / name
        content = json.dumps({"legacy": "gen1", "cash": 123.45, "name": name})
        p.write_text(content, encoding="utf-8")
        gen1[str(p)] = content

    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT", "ETHUSDT"))
    coord = make_coord(build_test_manifest(cfg), state, cfg, GrowingFetch(frames))
    coord.prepare()
    coord.run_tick(dry_run=True, now=ANCHOR)
    coord.set_status(Status.ACTIVE, approved=True)
    coord.run_tick(dry_run=False, now=ANCHOR + timedelta(hours=1))

    # Gen1 bytes unchanged.
    for path, content in gen1.items():
        assert open(path, encoding="utf-8").read() == content

    # Every file written lives under state/gen2/<id>/ (or is one of the Gen1 files).
    exp_dir = os.path.normpath(os.path.abspath(coord.exp_dir))
    for root, _dirs, files in os.walk(state):
        for f in files:
            full = os.path.normpath(os.path.abspath(os.path.join(root, f)))
            if full in {os.path.normpath(os.path.abspath(p)) for p in gen1}:
                continue
            assert full.startswith(exp_dir), f"gen2 wrote outside its namespace: {full}"

    # No new *_sim.json anywhere.
    sim_files = []
    for root, _dirs, files in os.walk(state):
        sim_files += [f for f in files if f.endswith("_sim.json")]
    assert sorted(sim_files) == ["momentum_BTCUSDT_sim.json", "rsi_ETHUSDT_sim.json"]
