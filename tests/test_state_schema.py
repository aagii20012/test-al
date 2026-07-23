"""Fail-closed generation / schema boundary (Decision 3).

The corrected runner must refuse to resume Generation 1 (invalidated, unmarked)
state or any non-current schema, must never overwrite a non-current file, and
must gate the first-ever fresh launch behind explicit approval.
"""

import json
from pathlib import Path

import pytest

from algotrading.cli import _save_tick_state
from algotrading.state_schema import (
    GENERATION,
    SCHEMA_VERSION,
    IncompatibleStateError,
    ensure_fresh_start_allowed,
    is_current,
    stamp,
    validate_loadable,
)

REPO = Path(__file__).resolve().parent.parent
GEN1_SNAPSHOT = REPO / "evidence" / "gen1" / "state_snapshot"


def _gen1_blob():
    # Shape of a Generation 1 checkpoint: real accounting, NO generation marker.
    return {
        "portfolio": {"cash": 10000.0, "positions": {}, "avg_price": {}},
        "risk": {},
        "strategy": {"pos": {"BTCUSDT": 1}},
        "last_bar_ts": 1_700_000_000_000,
    }


def test_gen1_unmarked_state_rejected():
    with pytest.raises(IncompatibleStateError) as ei:
        validate_loadable(_gen1_blob(), "state/momentum_BTCUSDT_sim.json")
    assert "Generation 1" in str(ei.value)


def test_wrong_schema_version_rejected():
    blob = stamp(_gen1_blob())
    blob["schema_version"] = SCHEMA_VERSION + 1  # future schema
    with pytest.raises(IncompatibleStateError):
        validate_loadable(blob, "x.json")


def test_wrong_generation_rejected():
    blob = _gen1_blob()
    blob["generation"] = "gen1"
    blob["schema_version"] = SCHEMA_VERSION
    with pytest.raises(IncompatibleStateError):
        validate_loadable(blob, "x.json")


def test_current_generation_state_loads():
    blob = stamp(_gen1_blob())
    assert is_current(blob)
    validate_loadable(blob, "x.json")  # must not raise


def test_fresh_start_requires_explicit_approval():
    with pytest.raises(IncompatibleStateError):
        ensure_fresh_start_allowed("state/new.json", allowed=False)
    ensure_fresh_start_allowed("state/new.json", allowed=True)  # must not raise


def test_save_refuses_to_overwrite_gen1_file_and_leaves_it_unchanged(tmp_path):
    victim = tmp_path / "gen1_state.json"
    original_bytes = json.dumps(_gen1_blob(), indent=2).encode("utf-8")
    victim.write_bytes(original_bytes)

    with pytest.raises(IncompatibleStateError):
        _save_tick_state(str(victim), {"portfolio": {}, "risk": {}})

    # The Generation 1 file must be byte-for-byte untouched.
    assert victim.read_bytes() == original_bytes


def test_save_creates_then_overwrites_own_gen2_file(tmp_path):
    path = tmp_path / "gen2_state.json"

    _save_tick_state(str(path), {"portfolio": {"cash": 1.0}, "risk": {}})
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["generation"] == GENERATION
    assert first["schema_version"] == SCHEMA_VERSION

    # A subsequent tick can overwrite its own current-generation checkpoint.
    _save_tick_state(str(path), {"portfolio": {"cash": 2.0}, "risk": {}})
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["portfolio"]["cash"] == 2.0
    assert is_current(second)


def test_frozen_gen1_evidence_is_rejected_and_never_overwritten(tmp_path):
    # Against genuine frozen Gen1 evidence: each file is rejected on load, and a
    # save attempt refuses and leaves the evidence byte-identical.
    snapshots = sorted(GEN1_SNAPSHOT.glob("*.json"))
    assert snapshots, "expected frozen Gen1 evidence to exist"
    for snap in snapshots:
        raw = snap.read_bytes()
        blob = json.loads(raw)
        assert not is_current(blob)
        with pytest.raises(IncompatibleStateError):
            validate_loadable(blob, str(snap))
        # And a write attempt against a copy of it must refuse + preserve bytes.
        copy = tmp_path / snap.name
        copy.write_bytes(raw)
        with pytest.raises(IncompatibleStateError):
            _save_tick_state(str(copy), {"portfolio": {}, "risk": {}})
        assert copy.read_bytes() == raw
