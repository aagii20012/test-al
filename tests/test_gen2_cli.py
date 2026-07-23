"""CLI-level tests for the Generation-2 operator entry point (Issues 2 & 3).

These prove the human-operated ``python -m algotrading.gen2`` surface behaves
exactly as the launch protocol requires, WITHOUT touching the network or
activating anything:

  * ``prepare --implementation-commit`` binds the manifest's ``code`` block to the
    operator-supplied (already-pushed) Stage-A commit — not the working-tree HEAD —
    while the experiment id / binding stay content-addressed (commit-independent).
  * ``verify-current`` resolves the single CURRENT pointer, re-verifies the
    checkpoint manifest + every artifact hash, and FAILS CLOSED (exit 2) on any
    tamper, printing a clean diagnostic rather than a stack trace.
  * ``preflight`` maps the provenance gate's verdict onto process exit codes:
    0 = certified, 2 = provenance failed, 3 = network blocked — so a scheduler /
    operator can branch on the result. (The gate itself is proven offline in
    test_gen2_provenance.py; here we only prove the CLI's exit-code mapping, so we
    stub the gate to avoid a live request.)

Nothing here activates an experiment, enables a scheduler, or writes CURRENT for a
live tick. Every checkpoint published below is an offline dry-run.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, MOMENTUM_PARAMS, GrowingFetch, build_test_manifest, make_coord,
    scripted_frames, write_config)

from algotrading.gen2 import __main__ as cli  # noqa: E402
from algotrading.gen2 import checkpoint as cp  # noqa: E402
from algotrading.gen2 import experiment as exp  # noqa: E402
from algotrading.gen2.provenance import ProvenanceIssue, ProvenanceReport  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _prepared_single(tmp_path):
    """Prepare a one-bot momentum/BTCUSDT experiment under tmp_path/state via the
    coordinator (bypassing the CLI's own prepare so we control the fetch), publish
    ONE dry-run checkpoint, and return (state_root, exp_id, coord, bot_id)."""
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT",))
    fetch = GrowingFetch(frames)
    m = build_test_manifest(cfg, symbols=("BTCUSDT",), strategies=("momentum",),
                            params_override={"momentum": MOMENTUM_PARAMS})
    state_root = str(tmp_path / "state")
    coord = make_coord(m, state_root, cfg, fetch)
    coord.prepare()
    n = len(frames["BTCUSDT"])
    fetch.upto = n - 1
    r = coord.run_tick(dry_run=True, now=ANCHOR + timedelta(hours=n - 1))
    assert r.status == "PUBLISHED"
    return state_root, m.experiment_id, coord, m.bots[0]["bot_id"]


def _report(*, issues=()):
    r = ProvenanceReport(
        checked_utc="2024-06-01T12:13:00+00:00", interval="1h",
        freshness_tolerance_s=7200, min_warmup=110, strict=True, source="network")
    r.issues.extend(issues)
    return r


# --------------------------------------------------------------------------
# prepare --implementation-commit (Issue 3)
# --------------------------------------------------------------------------
def test_prepare_binds_supplied_implementation_commit(tmp_path, capsys):
    cfg = write_config(tmp_path / "cfg.yaml")
    state_root = str(tmp_path / "state")
    commit = "0" * 40
    rc = cli.main(["--state-root", state_root, "--config", cfg,
                   "--no-verify-code", "prepare", "--implementation-commit", commit])
    assert rc == 0

    # exactly one experiment written; read its manifest back.
    ids = [os.path.basename(os.path.dirname(p))
           for p in _glob_manifests(state_root)]
    assert len(ids) == 1
    manifest = _load_manifest(state_root, ids[0])

    # the code block is bound to the SUPPLIED commit, not the working-tree HEAD.
    assert manifest.code["implementation_commit"] == commit
    # activation_commit is a separate, still-unset field (no self-reference).
    assert manifest.activation_commit is None
    # the immutable binding still verifies (code is inside it).
    manifest.verify_binding()

    out = capsys.readouterr().out
    assert commit in out and "PREPARED" in out


def test_experiment_id_is_commit_independent(tmp_path):
    """Two prepares that differ ONLY in --implementation-commit must produce the
    same experiment id (id is content-addressed on the source tree, not the
    commit) — but different bound implementation_commit values."""
    cfg = write_config(tmp_path / "cfg.yaml")
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    assert cli.main(["--state-root", a, "--config", cfg, "--no-verify-code",
                     "prepare", "--implementation-commit", "a" * 40]) == 0
    assert cli.main(["--state-root", b, "--config", cfg, "--no-verify-code",
                     "prepare", "--implementation-commit", "b" * 40]) == 0
    ida = os.path.basename(os.path.dirname(_glob_manifests(a)[0]))
    idb = os.path.basename(os.path.dirname(_glob_manifests(b)[0]))
    # The id is gen2-<utc-timestamp>-<content-hash>. The content-addressed suffix
    # (source tree + config + bot params + cost model) must be IDENTICAL regardless
    # of the bound commit; only the wall-clock timestamp segment may differ if the
    # two prepares crossed a one-second boundary.
    assert ida.split("-")[-1] == idb.split("-")[-1]     # commit-independent hash
    ma = _load_manifest(a, ida)
    mb = _load_manifest(b, idb)
    assert ma.code["implementation_commit"] == "a" * 40
    assert mb.code["implementation_commit"] == "b" * 40


def test_prepare_without_commit_falls_back(tmp_path, capsys):
    cfg = write_config(tmp_path / "cfg.yaml")
    state_root = str(tmp_path / "state")
    rc = cli.main(["--state-root", state_root, "--config", cfg,
                   "--no-verify-code", "prepare"])
    assert rc == 0
    ids = [os.path.basename(os.path.dirname(p))
           for p in _glob_manifests(state_root)]
    manifest = _load_manifest(state_root, ids[0])
    # falls back to the local HEAD marker (== git_commit()); may be None off-git.
    assert manifest.code["implementation_commit"] == exp.git_commit()
    assert "best-effort" in capsys.readouterr().out


# --------------------------------------------------------------------------
# verify-current (Issues 4 & 5: reader verifies CURRENT + hashes, fail closed)
# --------------------------------------------------------------------------
def test_verify_current_reports_no_checkpoint(tmp_path, capsys):
    cfg = write_config(tmp_path / "cfg.yaml")
    state_root = str(tmp_path / "state")
    assert cli.main(["--state-root", state_root, "--config", cfg,
                     "--no-verify-code", "prepare"]) == 0
    rc = cli.main(["--state-root", state_root, "--config", cfg,
                   "--no-verify-code", "verify-current"])
    assert rc == 0
    assert "no published checkpoint" in capsys.readouterr().out


def test_verify_current_certifies_clean_checkpoint(tmp_path, capsys):
    state_root, exp_id, _coord, _bot = _prepared_single(tmp_path)
    rc = cli.main(["--state-root", state_root, "--config",
                   str(tmp_path / "cfg.yaml"), "--no-verify-code",
                   "verify-current"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CURRENT verified" in out and "bots verified   : 1" in out


def test_verify_current_fails_closed_on_tampered_artifact(tmp_path, capsys):
    state_root, exp_id, coord, bot_id = _prepared_single(tmp_path)
    ref = coord.read_current()
    assert ref is not None
    artifact = os.path.join(coord.checkpoint_dir(ref.checkpoint),
                            cp.BOTS_DIRNAME, f"{bot_id}.json")
    with open(artifact, "ab") as fh:                    # append one byte -> hash breaks
        fh.write(b" ")

    rc = cli.main(["--state-root", state_root, "--config",
                   str(tmp_path / "cfg.yaml"), "--no-verify-code",
                   "verify-current"])
    assert rc == 2
    assert "INTEGRITY FAILURE" in capsys.readouterr().out


# --------------------------------------------------------------------------
# preflight exit-code mapping (Issue 2 CLI surface; gate stubbed)
# --------------------------------------------------------------------------
def test_preflight_ok_exit_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_provenance_gate",
                        lambda **k: _report())
    rc = cli.main(["--state-root", str(tmp_path / "state"), "preflight"])
    assert rc == 0
    assert "PROVENANCE OK" in capsys.readouterr().out


def test_preflight_failed_exit_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_run_provenance_gate",
        lambda **k: _report(issues=[ProvenanceIssue("freshness", "stale feed")]))
    rc = cli.main(["--state-root", str(tmp_path / "state"), "preflight"])
    assert rc == 2
    assert "MARKET_DATA_PROVENANCE_FAILED" in capsys.readouterr().out


def test_preflight_network_exit_three(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_run_provenance_gate",
        lambda **k: _report(issues=[ProvenanceIssue("network", "dns failure")]))
    rc = cli.main(["--state-root", str(tmp_path / "state"), "preflight"])
    assert rc == 3
    assert "BLOCKED" in capsys.readouterr().out


def test_preflight_json_mode_emits_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_provenance_gate", lambda **k: _report())
    rc = cli.main(["--state-root", str(tmp_path / "state"), "preflight", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.split("PROVENANCE OK")[0])
    assert payload["ok"] is True and payload["strict"] is True


# --------------------------------------------------------------------------
# tiny manifest-reading utilities (avoid importing the CLI's private helpers)
# --------------------------------------------------------------------------
def _glob_manifests(state_root):
    import glob
    from algotrading import state_schema
    return sorted(glob.glob(
        os.path.join(state_root, state_schema.GENERATION, "*", "manifest.json")))


def _load_manifest(state_root, exp_id):
    from algotrading import state_schema
    path = os.path.join(state_root, state_schema.GENERATION, exp_id, "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        return exp.ExperimentManifest.from_dict(json.load(fh))
