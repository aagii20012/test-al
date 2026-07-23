"""Gen2 scoreboard: sourced from ONE experiment dir, never from Generation 1.

A PREPARED experiment renders "not trading yet" (no live results even if a
dry-run tick published a marker). Only an ACTIVE experiment that has published a
tick shows live per-bot equity. The dashboard never opens a ``*_sim.json``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, GrowingFetch, build_test_manifest, make_coord, scripted_frames,
    write_config)

from algotrading.gen2 import dashboard  # noqa: E402
from algotrading.gen2.experiment import Status  # noqa: E402


def _prepared_coord(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT", "ETHUSDT"))
    coord = make_coord(build_test_manifest(cfg), tmp_path / "state", cfg,
                       GrowingFetch(frames))
    coord.prepare()
    return coord


# --------------------------------------------------------------------------
# PREPARED: listed but not trading
# --------------------------------------------------------------------------
def test_prepared_scoreboard_lists_bots_without_results(tmp_path):
    coord = _prepared_coord(tmp_path)
    sb = dashboard.build_scoreboard(coord.exp_dir)

    assert sb["status"] == "PREPARED"
    assert sb["trading"] is False
    assert sb["generation"] == "gen2"
    assert len(sb["bots"]) == 8
    assert all(b["has_results"] is False for b in sb["bots"])
    assert all(b["equity"] == 10_000.0 for b in sb["bots"])
    assert dashboard.GEN1_NOTICE in sb["gen1_notice"]


def test_dry_run_marker_does_not_flip_to_trading(tmp_path):
    coord = _prepared_coord(tmp_path)
    coord.run_tick(dry_run=True, now=ANCHOR)          # publishes a marker
    sb = dashboard.build_scoreboard(coord.exp_dir)

    # A PREPARED experiment is NOT "trading" even after a dry-run tick.
    assert sb["status"] == "PREPARED"
    assert sb["trading"] is False
    assert all(b["has_results"] is False for b in sb["bots"])


# --------------------------------------------------------------------------
# ACTIVE: live results shown, ranked
# --------------------------------------------------------------------------
def test_active_scoreboard_shows_live_results(tmp_path):
    coord = _prepared_coord(tmp_path)
    coord.set_status(Status.ACTIVE, approved=True)
    coord.run_tick(dry_run=False, now=ANCHOR)
    sb = dashboard.build_scoreboard(coord.exp_dir)

    assert sb["status"] == "ACTIVE"
    assert sb["trading"] is True
    assert all(b["has_results"] for b in sb["bots"])
    assert sb["decision_epoch_ms"] is not None
    assert sb["snapshot_sha256"]
    # ranked by equity descending
    equities = [b["equity"] for b in sb["bots"]]
    assert equities == sorted(equities, reverse=True)


# --------------------------------------------------------------------------
# isolation from Generation 1
# --------------------------------------------------------------------------
def test_dashboard_never_opens_gen1_files(tmp_path, monkeypatch):
    coord = _prepared_coord(tmp_path)
    # Plant Gen1 files right next to the state root.
    (tmp_path / "state" / "momentum_BTCUSDT_sim.json").write_text(
        json.dumps({"legacy": True}), encoding="utf-8")

    opened = []
    real_open = open

    def tracking_open(path, *a, **k):
        opened.append(os.path.abspath(str(path)))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", tracking_open)
    dashboard.build_scoreboard(coord.exp_dir)

    exp_dir = os.path.abspath(coord.exp_dir)
    for p in opened:
        assert p.startswith(exp_dir), f"dashboard read outside experiment: {p}"
    assert not any(p.endswith("_sim.json") for p in opened)


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        dashboard.build_scoreboard(str(tmp_path / "nope"))


# --------------------------------------------------------------------------
# rendered HTML
# --------------------------------------------------------------------------
def test_render_html_prepared_has_banner_and_gen1_notice(tmp_path):
    coord = _prepared_coord(tmp_path)
    html = dashboard.render_html(dashboard.build_scoreboard(coord.exp_dir))
    assert "PREPARED" in html
    assert "INVALIDATED" in html            # Gen1 warning banner
    assert "not yet" in html.lower()
    assert coord.manifest.experiment_id in html


def test_render_html_active_shows_equity(tmp_path):
    coord = _prepared_coord(tmp_path)
    coord.set_status(Status.ACTIVE, approved=True)
    coord.run_tick(dry_run=False, now=ANCHOR)
    html = dashboard.render_html(dashboard.build_scoreboard(coord.exp_dir))
    assert "ACTIVE" in html
    assert "$" in html and "%" in html
