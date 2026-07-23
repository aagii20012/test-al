"""Experiment identity + immutable binding manifest.

The manifest binds code + config + the 8-bot roster; any tamper of an immutable
field must be caught by ``verify_binding`` (fail closed). The experiment id must
follow ``gen2-YYYYMMDDTHHMMSSZ-<8hex>`` and the roster must be exactly the
approved 4 strategies x 2 coins, funded $10,000 each.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import ANCHOR, write_config  # noqa: E402

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import experiment as exp  # noqa: E402
from algotrading.gen2.experiment import (  # noqa: E402
    BOT_DEFS, ExperimentManifest, Status, build_manifest, make_experiment_id)

ID_RE = re.compile(r"^gen2-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _manifest(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    return build_manifest(created=ANCHOR, config_path=cfg)


# --------------------------------------------------------------------------
# identity + markers
# --------------------------------------------------------------------------
def test_experiment_id_format():
    assert ID_RE.match(make_experiment_id(ANCHOR, "deadbeefcafe"))


def test_manifest_markers_are_gen2(tmp_path):
    m = _manifest(tmp_path)
    assert m.generation == state_schema.GENERATION == "gen2"
    assert m.schema_version == state_schema.SCHEMA_VERSION == 2
    assert m.status == Status.PREPARED
    assert ID_RE.match(m.experiment_id)
    assert m.capital_per_bot == 10_000.0


def test_id_is_stable_for_same_inputs(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    a = build_manifest(created=ANCHOR, config_path=cfg)
    b = build_manifest(created=ANCHOR, config_path=cfg)
    assert a.experiment_id == b.experiment_id      # code+config+bots unchanged
    assert a.binding_sha256 == b.binding_sha256


# --------------------------------------------------------------------------
# the 8-bot roster
# --------------------------------------------------------------------------
def test_eight_bot_defs_shape(tmp_path):
    m = _manifest(tmp_path)
    assert len(m.bots) == 8
    ids = m.bot_ids()
    assert ids == [f"{s}_{sym}" for s in exp.STRATEGIES for sym in exp.SYMBOLS]
    assert len(set(ids)) == 8
    for b in m.bots:
        assert b["initial_capital"] == 10_000.0
        assert b["bot_id"] == f"{b['strategy']}_{b['symbol']}"
        assert b["product"] == exp.PRODUCTS[b["symbol"]]
        assert b["params_sha256"] == exp.sha256_canonical(b["params"])
    assert len(BOT_DEFS) == 8


# --------------------------------------------------------------------------
# immutability / fail-closed binding
# --------------------------------------------------------------------------
def test_clean_manifest_verifies(tmp_path):
    _manifest(tmp_path).verify_binding()          # no raise


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("capital_per_bot", 1.0),
    lambda d: d.__setitem__("experiment_id", "gen2-19700101T000000Z-00000000"),
    lambda d: d.__setitem__("generation", "gen1"),
    lambda d: d.__setitem__("schema_version", 1),
    lambda d: d["cost_model"].__setitem__("commission_pct", 0.0),
    lambda d: d["bots"][0]["params"].__setitem__("lookback", 3),
    lambda d: d["market"].__setitem__("history", 5),
])
def test_tampered_immutable_field_rejected(tmp_path, mutate):
    d = _manifest(tmp_path).to_dict()
    mutate(d)                                      # change a bound field, keep hash
    with pytest.raises(state_schema.IncompatibleStateError):
        ExperimentManifest.from_dict(d).verify_binding()


def test_status_is_not_bound(tmp_path):
    # status may legally change without breaking the binding hash.
    m = _manifest(tmp_path)
    d = m.to_dict()
    d["status"] = Status.ACTIVE
    d["history"] = [{"from": "PREPARED", "to": "ACTIVE", "approved": True}]
    ExperimentManifest.from_dict(d).verify_binding()   # no raise


def test_changing_code_hash_changes_id(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    m1 = build_manifest(created=ANCHOR, config_path=cfg)
    # A different bound config produces a different id + binding.
    cfg2 = write_config(tmp_path / "cfg2.yaml", financing_apr=0.99)
    m2 = build_manifest(created=ANCHOR, config_path=cfg2)
    assert m1.experiment_id != m2.experiment_id
