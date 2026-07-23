"""Crash-injection, checkpoint-integrity and orphan-recovery proofs (Issues 1 & 5).

The safety claim under test is the whole point of the immutable-checkpoint model:

    A process death at ANY point in the publish protocol leaves ``CURRENT``
    resolving to the COMPLETE old checkpoint or the COMPLETE new one — NEVER a
    mixed / half-written state — and a clean re-run afterwards converges to the
    byte-identical checkpoint a never-interrupted run would have produced.

The publish protocol (see ``coordinator._write_checkpoint_dir`` /
``_swing_current``) exposes an injectable ``_crash_hook`` fired at each step:

    after_stage_artifacts -> after_checkpoint_manifest -> after_fsync ->
    before_rename -> after_rename -> before_current -> during_current -> after_current

Everything up to and including ``during_current`` is BEFORE the single atomic
``os.replace`` of the CURRENT pointer, so a crash there must leave CURRENT at the
*old* value (complete-old). ``after_current`` is *after* the flip, so the store is
already complete-new (the tick is durably published even though the call raised).

Offline only; every run writes under a pytest ``tmp_path``; nothing activates
real trading, touches Generation 1, or reaches the network.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

import sys
sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, MOMENTUM_PARAMS, CrashAt, GrowingFetch, build_test_manifest,
    checkpoint_names, current_checkpoint, current_ref, make_coord,
    scripted_frames, write_config)

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import checkpoint as cp  # noqa: E402
from algotrading.gen2 import dashboard  # noqa: E402
from algotrading.gen2.experiment import Status  # noqa: E402
from algotrading.gen2.coordinator import StaleRunError  # noqa: E402

# Fixed tick timestamps: identical `now` for the crashed run, its recovery, and
# the reference run makes every stamped artifact byte-for-byte comparable.
NOW1 = ANCHOR + timedelta(hours=300)
NOW2 = ANCHOR + timedelta(hours=320)
UPTO1 = 150
UPTO2 = 160

# Stages that fire BEFORE the checkpoint dir is renamed into place: a crash here
# leaves at most an invisible `.staging-*` dir, never a materialised checkpoint.
BEFORE_RENAME = ("after_stage_artifacts", "after_checkpoint_manifest",
                 "after_fsync", "before_rename")
# Stages AFTER the rename but BEFORE the CURRENT flip: a complete ORPHAN
# checkpoint exists on disk but is not yet pointed-to (invisible to readers).
AFTER_RENAME = ("after_rename", "before_current", "during_current")
# Every stage strictly before the atomic CURRENT replace -> "complete-old".
PRE_CURRENT = BEFORE_RENAME + AFTER_RENAME


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _single(tmp_path, sub, cfg, frames, *, active=False):
    """A prepared one-bot (momentum/BTCUSDT) coordinator under ``tmp_path/sub``."""
    fetch = GrowingFetch(frames)
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, tmp_path / sub, cfg, fetch)
    coord.prepare()
    if active:
        coord.set_status(Status.ACTIVE, approved=True)
    return coord, fetch


def _dir_bytes(root):
    """Every file under ``root`` as ``relpath -> raw bytes`` (recursive)."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(base, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                out[rel] = fh.read()
    return out


def _staging(coord):
    root = coord.checkpoints_dir
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if n.startswith(cp.STAGING_PREFIX))


# --------------------------------------------------------------------------
# CRASH MATRIX — first tick (no prior checkpoint): complete-old == "nothing"
# --------------------------------------------------------------------------
@pytest.mark.parametrize("stage", PRE_CURRENT)
def test_crash_first_tick_publishes_nothing_then_recovers(tmp_path, stage):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))

    crash, cfetch = _single(tmp_path, "crash", cfg, frames)
    cfetch.upto = UPTO1

    # --- crash mid-publish ------------------------------------------------
    crash._crash_hook = CrashAt(stage)
    with pytest.raises(RuntimeError, match="injected crash"):
        crash.run_tick(dry_run=True, now=NOW1)

    # complete-old: no experiment has EVER published, so CURRENT is absent.
    assert current_ref(crash) is None
    assert current_checkpoint(crash) is None
    assert not os.path.exists(crash.current_path)

    names_after_crash = checkpoint_names(crash)
    if stage in BEFORE_RENAME:
        assert names_after_crash == []            # nothing renamed into place
    else:
        # A complete ORPHAN exists on disk but is invisible (CURRENT still absent).
        assert len(names_after_crash) == 1

    # --- clean re-run recovers -------------------------------------------
    crash._crash_hook = None
    res = crash.run_tick(dry_run=True, now=NOW1)
    assert res.status == "PUBLISHED"
    if stage in AFTER_RENAME:
        assert names_after_crash == [res.checkpoint]   # the orphan was reused
    assert current_ref(crash).checkpoint == res.checkpoint
    assert _staging(crash) == []                       # staging cleaned up

    # --- byte-identical to a never-crashed reference ---------------------
    ref, rfetch = _single(tmp_path, "ref", cfg, frames)
    rfetch.upto = UPTO1
    res_ref = ref.run_tick(dry_run=True, now=NOW1)
    assert res_ref.checkpoint == res.checkpoint
    assert (_dir_bytes(crash.checkpoint_dir(res.checkpoint))
            == _dir_bytes(ref.checkpoint_dir(res_ref.checkpoint)))
    # and the CURRENT pointers agree on the published checkpoint-manifest hash.
    assert (current_ref(crash).checkpoint_manifest_sha256
            == current_ref(ref).checkpoint_manifest_sha256)


# --------------------------------------------------------------------------
# CRASH MATRIX — second tick (has a prior): complete-old == the prior checkpoint
# --------------------------------------------------------------------------
@pytest.mark.parametrize("stage", PRE_CURRENT)
def test_crash_second_tick_keeps_prior_then_recovers(tmp_path, stage):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))

    crash, cfetch = _single(tmp_path, "crash", cfg, frames)
    ref, rfetch = _single(tmp_path, "ref", cfg, frames)

    # Both publish a clean C1.
    cfetch.upto = rfetch.upto = UPTO1
    c1 = crash.run_tick(dry_run=True, now=NOW1)
    c1_ref = ref.run_tick(dry_run=True, now=NOW1)
    assert c1.checkpoint == c1_ref.checkpoint

    # crash publishes C2 (a strictly newer boundary) but dies mid-publish.
    cfetch.upto = UPTO2
    crash._crash_hook = CrashAt(stage)
    with pytest.raises(RuntimeError, match="injected crash"):
        crash.run_tick(dry_run=True, now=NOW2)

    # complete-old: CURRENT still resolves to the fully-intact C1.
    cur = current_ref(crash)
    assert cur.checkpoint == c1.checkpoint
    ck = current_checkpoint(crash)
    assert ck.name == c1.checkpoint
    assert ck.boundary_epoch == c1.decision_epoch_ms

    names_after_crash = set(checkpoint_names(crash))
    if stage in BEFORE_RENAME:
        assert names_after_crash == {c1.checkpoint}          # no C2 dir
    else:
        assert len(names_after_crash) == 2                   # C1 + orphan C2

    # clean re-run recovers to the complete-new C2.
    crash._crash_hook = None
    c2 = crash.run_tick(dry_run=True, now=NOW2)
    assert c2.status == "PUBLISHED"
    assert c2.checkpoint != c1.checkpoint
    assert c2.prior_checkpoint == c1.checkpoint
    assert current_ref(crash).checkpoint == c2.checkpoint
    assert _staging(crash) == []

    # byte-identical C2 vs a never-crashed reference that ran C1 then C2.
    rfetch.upto = UPTO2
    c2_ref = ref.run_tick(dry_run=True, now=NOW2)
    assert c2_ref.checkpoint == c2.checkpoint
    assert (_dir_bytes(crash.checkpoint_dir(c2.checkpoint))
            == _dir_bytes(ref.checkpoint_dir(c2_ref.checkpoint)))


# --------------------------------------------------------------------------
# after_current — the flip already happened: complete-NEW, and idempotent
# --------------------------------------------------------------------------
def test_crash_after_current_is_already_published(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames)
    fetch.upto = UPTO1

    coord._crash_hook = CrashAt("after_current")
    with pytest.raises(RuntimeError, match="injected crash"):
        coord.run_tick(dry_run=True, now=NOW1)

    # The CURRENT replace already succeeded before the hook fired: complete-new.
    ck = current_checkpoint(coord)
    assert ck is not None
    published = ck.name

    # A clean re-run at the same boundary is an idempotent no-op.
    coord._crash_hook = None
    res = coord.run_tick(dry_run=True, now=NOW1 + timedelta(hours=1))
    assert res.status == "ALREADY_PUBLISHED"
    assert res.checkpoint == published
    assert current_ref(coord).checkpoint == published


# --------------------------------------------------------------------------
# CHECKPOINT-INTEGRITY — every tamper fails closed on the next read
# --------------------------------------------------------------------------
def _publish_c1(tmp_path, **kw):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames, **kw)
    fetch.upto = UPTO1
    now = NOW1
    if kw.get("active"):
        res = coord.run_tick(dry_run=False, now=now)
    else:
        res = coord.run_tick(dry_run=True, now=now)
    return coord, fetch, res


def test_corrupt_current_pointer_fails_closed(tmp_path):
    coord, _, _ = _publish_c1(tmp_path)
    with open(coord.current_path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    # A broken pointer is NEVER read as "no published state".
    with pytest.raises(cp.CheckpointError):
        current_ref(coord)
    with pytest.raises(cp.CheckpointError):
        current_checkpoint(coord)


def test_current_pointing_at_missing_checkpoint_fails_closed(tmp_path):
    coord, _, res = _publish_c1(tmp_path)
    ref = json.loads(open(coord.current_path, encoding="utf-8").read())
    ref["checkpoint"] = "9999999999999-" + "0" * 64      # nonexistent
    with open(coord.current_path, "w", encoding="utf-8") as fh:
        json.dump(ref, fh)
    with pytest.raises(cp.CheckpointError):
        current_checkpoint(coord)


def test_corrupted_checkpoint_artifact_fails_closed(tmp_path):
    coord, _, res = _publish_c1(tmp_path)
    cdir = coord.checkpoint_dir(res.checkpoint)
    bot_file = os.path.join(cdir, cp.BOTS_DIRNAME, "momentum_BTCUSDT.json")
    with open(bot_file, "ab") as fh:
        fh.write(b" ")                                   # one byte -> hash breaks
    with pytest.raises(cp.CheckpointError, match="corrupted"):
        current_checkpoint(coord)


def test_altered_checkpoint_manifest_fails_closed(tmp_path):
    coord, _, res = _publish_c1(tmp_path)
    cdir = coord.checkpoint_dir(res.checkpoint)
    mpath = os.path.join(cdir, cp.CHECKPOINT_MANIFEST_NAME)
    with open(mpath, "ab") as fh:
        fh.write(b"\n")            # changes the manifest bytes -> hash != CURRENT
    with pytest.raises(cp.CheckpointError):
        current_checkpoint(coord)


# --------------------------------------------------------------------------
# ORPHAN RECOVERY — a non-matching orphan is refused, NOT deleted
# --------------------------------------------------------------------------
def test_orphan_recovery_mismatch_refuses_and_preserves(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames)
    fetch.upto = UPTO1

    # Crash after the rename to leave a complete ORPHAN of a dry-run tick.
    coord._crash_hook = CrashAt("after_rename")
    with pytest.raises(RuntimeError):
        coord.run_tick(dry_run=True, now=NOW1)
    orphans = checkpoint_names(coord)
    assert len(orphans) == 1
    orphan = orphans[0]
    orphan_dir = coord.checkpoint_dir(orphan)

    # Tamper the orphan so it no longer matches the intended publication: flip
    # its recorded dry_run. (Artifacts + self-name stay intact, so it passes
    # structural checks and fails only the "is this exactly my tick?" gate.)
    mpath = os.path.join(orphan_dir, cp.CHECKPOINT_MANIFEST_NAME)
    m = json.loads(open(mpath, encoding="utf-8").read())
    m["dry_run"] = (not m["dry_run"])
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2, sort_keys=True)

    # Re-run the SAME (dry-run) tick: the orphan no longer matches -> fail closed.
    coord._crash_hook = None
    with pytest.raises(cp.CheckpointError, match="orphan"):
        coord.run_tick(dry_run=True, now=NOW1)

    # Crucially, the orphan was NOT silently deleted (investigate manually).
    assert os.path.isdir(orphan_dir)
    assert checkpoint_names(coord) == [orphan]
    # and nothing was published (the mismatch blocked the flip).
    assert current_ref(coord) is None


def test_matching_orphan_is_reused_not_rebuilt(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames)
    fetch.upto = UPTO1

    coord._crash_hook = CrashAt("before_current")   # orphan exists, CURRENT unset
    with pytest.raises(RuntimeError):
        coord.run_tick(dry_run=True, now=NOW1)
    orphan = checkpoint_names(coord)[0]
    before = _dir_bytes(coord.checkpoint_dir(orphan))

    coord._crash_hook = None
    res = coord.run_tick(dry_run=True, now=NOW1 + timedelta(hours=5))
    # Same content-addressed name, reused verbatim (recovery `now` differs, yet
    # the orphan's bytes are untouched — a matching orphan is never rewritten).
    assert res.checkpoint == orphan
    assert _dir_bytes(coord.checkpoint_dir(orphan)) == before
    assert current_ref(coord).checkpoint == orphan


# --------------------------------------------------------------------------
# DASHBOARD ignores orphan / unpublished checkpoints
# --------------------------------------------------------------------------
def test_dashboard_shows_only_current_ignores_orphan(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames, active=True)

    # Publish a live C1.
    fetch.upto = UPTO1
    c1 = coord.run_tick(dry_run=False, now=NOW1)

    # A newer tick dies after the rename -> a complete but UNPUBLISHED orphan C2.
    fetch.upto = UPTO2
    coord._crash_hook = CrashAt("after_rename")
    with pytest.raises(RuntimeError):
        coord.run_tick(dry_run=False, now=NOW2)
    assert len(checkpoint_names(coord)) == 2      # C1 + orphan C2 on disk

    # The dashboard resolves CURRENT only: it must show C1, never the orphan.
    board = dashboard.build_scoreboard(coord.exp_dir)
    assert board["status"] == "ACTIVE"
    assert board["trading"] is True
    assert board["checkpoint"] == c1.checkpoint
    assert board["decision_epoch_ms"] == c1.decision_epoch_ms
    # the one bot has live results sourced from C1.
    assert len(board["bots"]) == 1
    assert board["bots"][0]["has_results"] is True


# --------------------------------------------------------------------------
# STALE COORDINATOR — an older boundary can never regress CURRENT
# --------------------------------------------------------------------------
def test_stale_coordinator_cannot_regress_current(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames)

    # Publish C2 at the newer boundary first.
    fetch.upto = UPTO2
    c2 = coord.run_tick(dry_run=True, now=NOW2)

    # A stale run whose fetched window only reaches the OLDER boundary must be
    # refused before it computes or publishes anything.
    fetch.upto = UPTO1
    with pytest.raises(StaleRunError):
        coord.run_tick(dry_run=True, now=NOW1)

    # CURRENT is untouched — still C2.
    assert current_ref(coord).checkpoint == c2.checkpoint
    assert checkpoint_names(coord) == [c2.checkpoint]


# --------------------------------------------------------------------------
# _swing_current TOCTOU — a concurrent newer publish makes the flip fail closed
# --------------------------------------------------------------------------
def test_swing_current_loses_race_fails_closed(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    coord, fetch = _single(tmp_path, "state", cfg, frames)

    # Publish C1.
    fetch.upto = UPTO1
    c1 = coord.run_tick(dry_run=True, now=NOW1)

    # Prepare C2, but just before this run flips CURRENT, a *concurrent* newer
    # coordinator wins the race by advancing CURRENT to a higher boundary. Our
    # run must re-read that, see it is now behind, and refuse to clobber it.
    fetch.upto = UPTO2

    def race(stage):
        if stage == "before_current":
            competing = cp.CurrentRef(
                experiment_id=coord.manifest.experiment_id,
                checkpoint="9999999999999-" + "0" * 64,
                checkpoint_manifest_sha256="f" * 64,
                boundary_epoch=c1.decision_epoch_ms + 10 * 3600 * 1000,
                snapshot_sha256="0" * 64, prior_checkpoint=c1.checkpoint,
                dry_run=True, published_utc=NOW2.isoformat())
            with open(coord.current_path, "w", encoding="utf-8") as fh:
                json.dump(competing.to_dict(), fh)
        # do NOT raise: let _swing_current re-read and decide.

    coord._crash_hook = race
    with pytest.raises(StaleRunError):
        coord.run_tick(dry_run=True, now=NOW2)
