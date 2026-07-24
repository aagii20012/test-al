"""Behavioural tests for the deterministic ``replay-current`` control action.

``replay-current`` is the SAFE replacement for the old, racy operator step
"dispatch canary a second time and expect ALREADY_PUBLISHED". That step was not
deterministic: if a brand-new common hourly candle appeared between the two
dispatches, the second canary published that NEW boundary instead of proving the
already-authorised boundary is idempotent.

``replay-current`` removes the race by construction — it re-feeds the coordinator
the STORED immutable market snapshot for the boundary ``CURRENT`` already
published, never the live window. These tests prove it:

  * cannot run without a published CURRENT;
  * requires status ACTIVE (and a valid, non-retired, committed id);
  * replays the EXACT stored boundary, never the latest available one;
  * uses the stored snapshot and never touches the network;
  * returns ALREADY_PUBLISHED;
  * creates no new checkpoint and leaves the whole experiment directory
    byte-for-byte identical (so there is nothing to commit / no dashboard change);
  * fails closed if CURRENT or the checkpoint contents are corrupt.

All offline: no network, no real activation beyond the local temp state root.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _gen2_helpers import (  # noqa: E402
    ANCHOR, MOMENTUM_PARAMS, GrowingFetch, build_test_manifest,
    checkpoint_names, current_checkpoint, current_ref, make_coord,
    make_snapshot, scripted_frames, write_config)

from algotrading.gen2.experiment import Status  # noqa: E402
from tools import gen2_control  # noqa: E402


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _active_experiment(tmp_path):
    """A PREPARED->ACTIVE one-bot experiment on disk, with NO tick yet (no CURRENT)."""
    cfg = write_config(tmp_path / "cfg.yaml")
    state_root = tmp_path / "state"
    fetch = GrowingFetch(scripted_frames(("BTCUSDT",)))
    fetch.upto = 160
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    coord = make_coord(m, state_root, cfg, fetch)
    coord.prepare()
    coord.set_status(Status.ACTIVE, approved=True)
    return coord, m, cfg, state_root, fetch


def _active_with_current(tmp_path):
    """As above, then publish ONE real live checkpoint so CURRENT exists."""
    coord, m, cfg, state_root, fetch = _active_experiment(tmp_path)
    r = coord.run_tick(dry_run=False, now=ANCHOR)
    assert r.status == "PUBLISHED"
    return coord, m, cfg, state_root, fetch


def _forbid_live_fetch(monkeypatch):
    """Make ANY attempt to build/use the live market client fail loudly.

    ``replay-current`` injects the stored snapshot as the coordinator's fetch, so
    the default ``PublicMarketData`` path must never be reached. If it is, that is
    a real defect (live market consulted), so we raise rather than refuse.
    """
    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError(
                "replay-current constructed a live market client — it must only "
                "ever read the stored immutable snapshot.")

        def fetch_ohlcv(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("replay-current fetched live candles.")

    monkeypatch.setattr("algotrading.data.public.PublicMarketData", _Boom)


def _replay(state_root, experiment_id, cfg):
    return gen2_control.replay_current(
        str(state_root), experiment_id, str(cfg), verify_code=False)


# --------------------------------------------------------------------------
# preconditions: CURRENT + status + identity
# --------------------------------------------------------------------------
def test_replay_refuses_without_current(tmp_path):
    # ACTIVE but never ticked -> no published boundary to replay.
    coord, m, cfg, state_root, _ = _active_experiment(tmp_path)
    assert current_ref(coord) is None
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, m.experiment_id, cfg)
    assert "CURRENT" in ei.value.reason
    # Nothing was created by the refusal.
    assert current_ref(coord) is None
    assert checkpoint_names(coord) == []


def test_replay_refuses_when_not_active(tmp_path):
    # A published CURRENT exists, but the experiment is PAUSED, not ACTIVE.
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    coord.set_status(Status.PAUSED)
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, m.experiment_id, cfg)
    assert "requires status" in ei.value.reason


def test_replay_refuses_unknown_experiment(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    state_root = tmp_path / "state"
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, "gen2-does-not-exist", cfg)
    assert "unknown experiment id" in ei.value.reason


def test_replay_refuses_retired_experiment(tmp_path, monkeypatch):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    monkeypatch.setattr(gen2_control, "RETIRED_EXPERIMENT_IDS",
                        frozenset({m.experiment_id}))
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, m.experiment_id, cfg)
    assert "retired" in ei.value.reason


# --------------------------------------------------------------------------
# the core guarantee: exact stored boundary, no network, ALREADY_PUBLISHED
# --------------------------------------------------------------------------
def test_replay_returns_already_published(tmp_path):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    published = current_ref(coord)
    report = _replay(state_root, m.experiment_id, cfg)
    assert report["status"] == "ALREADY_PUBLISHED"
    assert report["checkpoint"] == published.checkpoint
    assert report["boundary_epoch"] == published.boundary_epoch
    assert report["snapshot_sha256"] == published.snapshot_sha256
    assert report["idempotency_key"] == \
        f"{m.experiment_id}:{published.boundary_epoch}"


def test_replay_pins_stored_boundary_not_latest(tmp_path, monkeypatch):
    # Publish CURRENT at an early boundary...
    coord, m, cfg, state_root, fetch = _active_with_current(tmp_path)
    published = current_ref(coord)
    early = published.boundary_epoch

    # ...then let the market move on: a strictly NEWER common boundary is now
    # available. If replay looked at the live window it would pick this one.
    later = make_snapshot(coord, fetch, upto=200)
    assert later.shared_candle_epoch_ms > early

    # Any live fetch during replay is a defect; make it explode.
    _forbid_live_fetch(monkeypatch)

    report = _replay(state_root, m.experiment_id, cfg)
    # It replayed the EXACT stored boundary, never the newer available one.
    assert report["status"] == "ALREADY_PUBLISHED"
    assert report["boundary_epoch"] == early
    assert report["boundary_epoch"] != later.shared_candle_epoch_ms


def test_replay_uses_stored_snapshot_never_the_network(tmp_path, monkeypatch):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    _forbid_live_fetch(monkeypatch)          # constructing PublicMarketData -> raise
    report = _replay(state_root, m.experiment_id, cfg)
    assert report["status"] == "ALREADY_PUBLISHED"


# --------------------------------------------------------------------------
# inertness: no new checkpoint, byte-identical directory, no leftover lock
# --------------------------------------------------------------------------
def test_replay_creates_no_new_checkpoint(tmp_path):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    names_before = checkpoint_names(coord)
    assert len(names_before) == 1
    _replay(state_root, m.experiment_id, cfg)
    assert checkpoint_names(coord) == names_before


def test_replay_leaves_experiment_dir_byte_identical(tmp_path):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    before = gen2_control._dir_fingerprint(coord.exp_dir)
    report = _replay(state_root, m.experiment_id, cfg)
    after = gen2_control._dir_fingerprint(coord.exp_dir)
    # Every file — CURRENT, checkpoint manifest, bot states, audit, run_status,
    # snapshot — hashes identically, and the file SET is unchanged.
    assert after == before
    assert report["files_verified_identical"] == len(before)
    # No transient run lock / tmp / staging directory survived.
    assert not os.path.exists(coord.lock_path)


# --------------------------------------------------------------------------
# fail-closed on corruption
# --------------------------------------------------------------------------
def test_replay_refuses_corrupt_current_pointer(tmp_path):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    with open(coord.current_path, "w", encoding="utf-8") as fh:
        fh.write("{ not valid json at all")
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, m.experiment_id, cfg)
    assert "corrupt" in ei.value.reason.lower()


def test_replay_refuses_corrupt_checkpoint_contents(tmp_path):
    coord, m, cfg, state_root, _ = _active_with_current(tmp_path)
    ckpt = current_checkpoint(coord)
    # Tamper ONE artifact so its sha256 no longer matches CHECKPOINT_MANIFEST.
    # CURRENT itself is untouched, so this fails at checkpoint verification.
    victim = os.path.join(ckpt.dir, "run_status.json")
    with open(victim, "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(gen2_control.ControlRefused) as ei:
        _replay(state_root, m.experiment_id, cfg)
    reason = ei.value.reason.lower()
    assert "integrity" in reason or "verif" in reason
