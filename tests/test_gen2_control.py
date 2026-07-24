"""Tests for the Generation-2 manual control plane.

Covers three surfaces added for the GitHub-web-only operator flow:

  1. tools/gen2_control.py    — the read-only ``guard`` legality gate, the
     deterministic read-only ``replay-current`` idempotency proof, and the
     ACTIVE->PAUSED ``pause`` transition (the lifecycle moves the engine CLI does
     not already expose).
  2. .github/workflows/gen2-control.yml — static safety assertions on the manual-
     control workflow (dispatch-only, least-privilege, scoped commit, no force,
     input passed via env not interpolated, cron disabled).
  3. The immutable binding of the committed PREPARED experiment — a regression
     guard proving these out-of-tree additions did NOT change the bound
     ``algotrading`` source hash, so the exact experiment stays activatable.

All offline: no network, no real activation, no tick.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _gen2_helpers import build_test_manifest, write_config  # noqa: E402

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import source_hash as sh  # noqa: E402
from algotrading.gen2.experiment import Status  # noqa: E402
from tools import gen2_control  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = os.path.join(_REPO_ROOT, ".github", "workflows", "gen2-control.yml")
# The single committed PREPARED experiment this whole capability is built to let
# an operator activate through the GitHub web UI.
_PREPARED_ID = "gen2-20260724T044436Z-01a99bd6"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _write_manifest(state_root, config_path, status):
    """Write a binding-consistent manifest at ``status`` under a temp state root."""
    m = build_test_manifest(str(config_path), status=status)
    exp_dir = os.path.join(str(state_root), state_schema.GENERATION, m.experiment_id)
    os.makedirs(exp_dir, exist_ok=True)
    with open(os.path.join(exp_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(m.to_dict(), fh)
    return m


def _read_status(state_root, experiment_id):
    path = os.path.join(str(state_root), state_schema.GENERATION, experiment_id,
                        "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["status"]


@pytest.fixture
def env(tmp_path):
    cfg = write_config(tmp_path / "config.yaml")
    state_root = tmp_path / "state"
    return {"config": cfg, "state_root": state_root}


# --------------------------------------------------------------------------
# guard — the legality matrix
# --------------------------------------------------------------------------
# (action, status, is_legal)
_MATRIX = [
    ("activate", Status.PREPARED, True),
    ("activate", Status.ACTIVE, False),
    ("activate", Status.PAUSED, False),
    ("activate", Status.FAILED, False),
    ("canary", Status.ACTIVE, True),
    ("canary", Status.PREPARED, False),
    ("canary", Status.PAUSED, False),
    ("replay-current", Status.ACTIVE, True),
    ("replay-current", Status.PREPARED, False),
    ("replay-current", Status.PAUSED, False),
    ("replay-current", Status.FAILED, False),
    ("pause", Status.ACTIVE, True),
    ("pause", Status.PREPARED, False),
    ("pause", Status.PAUSED, False),
    ("verify", Status.PREPARED, True),
    ("verify", Status.ACTIVE, True),
    ("verify", Status.PAUSED, True),
    ("verify", Status.FAILED, True),
]


@pytest.mark.parametrize("action,status,legal", _MATRIX)
def test_guard_legality_matrix(env, action, status, legal):
    m = _write_manifest(env["state_root"], env["config"], status)
    if legal:
        got = gen2_control.guard(action, str(env["state_root"]), m.experiment_id)
        assert got.experiment_id == m.experiment_id
    else:
        with pytest.raises(gen2_control.ControlRefused):
            gen2_control.guard(action, str(env["state_root"]), m.experiment_id)


@pytest.mark.parametrize("terminal", [Status.FAILED_CANARY, Status.CLOSED])
@pytest.mark.parametrize("action", gen2_control.ACTIONS)
def test_guard_refuses_every_action_on_terminal(env, action, terminal):
    m = _write_manifest(env["state_root"], env["config"], terminal)
    with pytest.raises(gen2_control.ControlRefused) as ei:
        gen2_control.guard(action, str(env["state_root"]), m.experiment_id)
    assert "terminal" in str(ei.value.reason)


def test_guard_refuses_unknown_experiment(env):
    with pytest.raises(gen2_control.ControlRefused) as ei:
        gen2_control.guard("activate", str(env["state_root"]),
                           "gen2-does-not-exist")
    assert "unknown experiment id" in str(ei.value.reason)


def test_guard_refuses_retired_experiment(env, monkeypatch):
    m = _write_manifest(env["state_root"], env["config"], Status.PREPARED)
    # Treat this experiment as retired without forging the real retired id's
    # content-derived binding; exercises the retired-refusal branch directly.
    monkeypatch.setattr(gen2_control, "RETIRED_EXPERIMENT_IDS",
                        frozenset({m.experiment_id}))
    for action in gen2_control.ACTIONS:
        with pytest.raises(gen2_control.ControlRefused) as ei:
            gen2_control.guard(action, str(env["state_root"]), m.experiment_id)
        assert "retired" in str(ei.value.reason)


def test_guard_refuses_tampered_binding(env):
    m = _write_manifest(env["state_root"], env["config"], Status.PREPARED)
    path = os.path.join(str(env["state_root"]), state_schema.GENERATION,
                        m.experiment_id, "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["capital_per_bot"] = data["capital_per_bot"] + 1  # mutate a bound field
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    with pytest.raises(gen2_control.ControlRefused) as ei:
        gen2_control.guard("activate", str(env["state_root"]), m.experiment_id)
    assert "does not verify" in str(ei.value.reason)


def test_guard_rejects_unknown_action(env):
    m = _write_manifest(env["state_root"], env["config"], Status.PREPARED)
    with pytest.raises(gen2_control.ControlRefused) as ei:
        gen2_control.guard("delete", str(env["state_root"]), m.experiment_id)
    assert "unknown action" in str(ei.value.reason)


# --------------------------------------------------------------------------
# guard — via the CLI (exit codes the workflow relies on)
# --------------------------------------------------------------------------
def test_cli_guard_ok_returns_zero(env, capsys):
    m = _write_manifest(env["state_root"], env["config"], Status.PREPARED)
    rc = gen2_control.main(["--state-root", str(env["state_root"]),
                            "guard", "--action", "activate",
                            "--experiment-id", m.experiment_id])
    assert rc == 0
    assert "GEN2_CONTROL_GUARD_OK" in capsys.readouterr().out


def test_cli_guard_refusal_returns_two(env, capsys):
    m = _write_manifest(env["state_root"], env["config"], Status.PREPARED)
    rc = gen2_control.main(["--state-root", str(env["state_root"]),
                            "guard", "--action", "canary",
                            "--experiment-id", m.experiment_id])
    assert rc == 2
    assert "GEN2_CONTROL_REFUSED" in capsys.readouterr().err


def test_cli_guard_rejects_out_of_vocabulary_action(env):
    # argparse choices=... rejects anything outside the closed action set.
    with pytest.raises(SystemExit):
        gen2_control.main(["guard", "--action", "rm-rf",
                           "--experiment-id", "whatever"])


# --------------------------------------------------------------------------
# pause — ACTIVE -> PAUSED, and refusals
# --------------------------------------------------------------------------
def test_pause_active_transitions_to_paused(env):
    m = _write_manifest(env["state_root"], env["config"], Status.ACTIVE)
    rc = gen2_control.main(["--state-root", str(env["state_root"]),
                            "--config", str(env["config"]),
                            "pause", "--experiment-id", m.experiment_id])
    assert rc == 0
    assert _read_status(env["state_root"], m.experiment_id) == Status.PAUSED


def test_pause_records_audited_transition(env):
    m = _write_manifest(env["state_root"], env["config"], Status.ACTIVE)
    gen2_control.main(["--state-root", str(env["state_root"]),
                       "--config", str(env["config"]),
                       "pause", "--experiment-id", m.experiment_id])
    path = os.path.join(str(env["state_root"]), state_schema.GENERATION,
                        m.experiment_id, "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        hist = json.load(fh)["history"]
    assert hist[-1] == {"from": Status.ACTIVE, "to": Status.PAUSED,
                        "approved": False}


@pytest.mark.parametrize("status", [Status.PREPARED, Status.PAUSED,
                                    Status.FAILED, Status.CLOSED])
def test_pause_refuses_non_active(env, status):
    m = _write_manifest(env["state_root"], env["config"], status)
    rc = gen2_control.main(["--state-root", str(env["state_root"]),
                            "--config", str(env["config"]),
                            "pause", "--experiment-id", m.experiment_id])
    assert rc == 2
    # State is untouched.
    assert _read_status(env["state_root"], m.experiment_id) == status


def test_pause_refuses_retired(env, monkeypatch):
    m = _write_manifest(env["state_root"], env["config"], Status.ACTIVE)
    monkeypatch.setattr(gen2_control, "RETIRED_EXPERIMENT_IDS",
                        frozenset({m.experiment_id}))
    rc = gen2_control.main(["--state-root", str(env["state_root"]),
                            "--config", str(env["config"]),
                            "pause", "--experiment-id", m.experiment_id])
    assert rc == 2
    assert _read_status(env["state_root"], m.experiment_id) == Status.ACTIVE


# --------------------------------------------------------------------------
# the workflow — static safety assertions
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def workflow():
    with open(_WORKFLOW, "r", encoding="utf-8") as fh:
        raw = fh.read()
    doc = yaml.safe_load(raw)
    return raw, doc


def _on_block(doc):
    # PyYAML parses the bare key ``on`` as the boolean True (YAML 1.1); accept both.
    return doc.get("on", doc.get(True))


def test_workflow_is_dispatch_only_no_cron(workflow):
    _, doc = workflow
    on = _on_block(workflow[1])
    assert isinstance(on, dict) and set(on.keys()) == {"workflow_dispatch"}
    # Cron and event-driven auto-triggers are absent by construction.
    assert "schedule" not in on
    assert "repository_dispatch" not in on
    assert "push" not in on


def test_workflow_action_input_is_closed_set(workflow):
    on = _on_block(workflow[1])
    opts = on["workflow_dispatch"]["inputs"]["action"]["options"]
    assert opts == ["activate", "canary", "replay-current", "verify", "pause"]
    assert on["workflow_dispatch"]["inputs"]["action"]["type"] == "choice"
    assert "experiment_id" in on["workflow_dispatch"]["inputs"]


def test_workflow_default_permissions_are_read_only(workflow):
    _, doc = workflow
    assert doc["permissions"] == {"contents": "read"}


def test_workflow_concurrency_keyed_by_experiment(workflow):
    _, doc = workflow
    assert "experiment_id" in doc["concurrency"]["group"]


def test_workflow_only_mutate_job_can_write(workflow):
    _, doc = workflow
    jobs = doc["jobs"]
    assert jobs["guard"]["permissions"] == {"contents": "read"}
    assert jobs["verify"]["permissions"] == {"contents": "read"}
    # replay-current is a read-only proof: it must never be granted write access.
    assert jobs["replay"]["permissions"] == {"contents": "read"}
    assert jobs["mutate"]["permissions"] == {"contents": "write"}


def test_workflow_verify_and_mutate_need_guard(workflow):
    _, doc = workflow
    jobs = doc["jobs"]
    assert jobs["verify"]["needs"] == "guard"
    assert jobs["replay"]["needs"] == "guard"
    assert jobs["mutate"]["needs"] == "guard"
    # The read and write paths are mutually exclusive on the action.
    assert "verify" in jobs["verify"]["if"]
    assert jobs["replay"]["if"] == "inputs.action == 'replay-current'"
    # mutate must exclude BOTH read-only actions, so replay-current never writes.
    assert "verify" in jobs["mutate"]["if"]
    assert "replay-current" in jobs["mutate"]["if"]


def test_workflow_passes_input_via_env_not_shell_interpolation(workflow):
    raw, _ = workflow
    # The free-text experiment id must never be interpolated straight into a shell
    # command (script-injection). It is bound to an env var and referenced as one.
    assert 'EXPERIMENT_ID: ${{ inputs.experiment_id }}' in raw
    assert '--experiment-id "$EXPERIMENT_ID"' in raw
    assert '--experiment-id "${{ inputs.experiment_id }}"' not in raw


def test_workflow_never_force_pushes_and_scopes_commits(workflow):
    raw, _ = workflow
    # No force-push in any of its command forms. (A comment may *say* "never
    # --force"; we only forbid the actual dangerous git invocations.)
    assert "push --force" not in raw
    assert "push -f" not in raw
    assert "--force-with-lease" not in raw
    assert "push origin +" not in raw          # `+refspec` = forced update
    assert 'EXP_PATH="state/gen2/${EXPERIMENT_ID}"' in raw
    # A rebase-then-revalidate publish path is present.
    assert "git rebase origin/main" in raw
    assert "verify-current" in raw


def test_workflow_references_no_secrets(workflow):
    raw, _ = workflow
    assert "secrets." not in raw


# --------------------------------------------------------------------------
# THE binding-integrity guard: adding this capability must not have changed the
# bound source hash of the committed PREPARED experiment (else it is no longer
# activatable). This test lives in CI forever as that regression tripwire.
# --------------------------------------------------------------------------
def test_committed_prepared_experiment_still_activatable():
    manifest_path = os.path.join(_REPO_ROOT, "state", state_schema.GENERATION,
                                 _PREPARED_ID, "manifest.json")
    if not os.path.exists(manifest_path):
        pytest.skip("committed PREPARED experiment not present in this checkout")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        code = json.load(fh)["code"]
    running = sh.canonical_source_hash()
    assert running["sha256"] == code["source_tree_sha256"], (
        "The bound algotrading source hash changed — the PREPARED experiment is "
        "no longer activatable. An out-of-tree-only change was expected.")
    assert running["inventory_sha256"] == code["source_inventory_sha256"]
    assert running["file_count"] == code["source_file_count"]
    assert running["algorithm"] == code["source_hash_algorithm"]
    assert running["version"] == code["source_hash_version"]
