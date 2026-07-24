"""Gen2 deployed (GitHub Pages) dashboard.

The deployed board differs from the offline preview: it carries a permanent
"simulated paper trading — no real money" disclaimer, shows the last successful
decision boundary + the AGE of that data, warns when the data is stale, never
uses "validated"/"recommended" language, and fails closed on a corrupt store
(so the last-good published site is never replaced by an unverifiable one). It
still reads ONLY the verified CURRENT checkpoint.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest


def _has_hype(html_lower: str) -> bool:
    """True if the page uses endorsement language.

    Word-boundary matched so the standing Gen1 "INVALIDATED" notice (which
    contains the substring "validated") is NOT a false positive.
    """
    return bool(re.search(r"\b(validated|recommended)\b", html_lower))

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import (  # noqa: E402
    ANCHOR, GrowingFetch, build_test_manifest, make_coord, scripted_frames,
    write_config)

from algotrading.gen2 import checkpoint as cp  # noqa: E402
from algotrading.gen2 import dashboard  # noqa: E402
from algotrading.gen2.experiment import Status  # noqa: E402
from algotrading.gen2.__main__ import _deployable_experiment_id  # noqa: E402


def _prepared_coord(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    frames = scripted_frames(("BTCUSDT", "ETHUSDT"))
    coord = make_coord(build_test_manifest(cfg), tmp_path / "state", cfg,
                       GrowingFetch(frames))
    coord.prepare()
    return coord


def _active_coord_with_tick(tmp_path):
    coord = _prepared_coord(tmp_path)
    coord.set_status(Status.ACTIVE, approved=True)
    coord.run_tick(dry_run=False, now=ANCHOR)
    return coord


# --------------------------------------------------------------------------
# disclaimer + no-hype language (Message B §6)
# --------------------------------------------------------------------------
def test_pages_html_has_paper_disclaimer_and_no_hype(tmp_path):
    coord = _active_coord_with_tick(tmp_path)
    sb = dashboard.build_scoreboard(coord.exp_dir)
    html = dashboard.render_pages_html(sb)

    low = html.lower()
    assert "simulated paper trading" in low
    assert "no real money" in low
    # Never imply endorsement (word-boundary: "invalidated" is allowed).
    assert not _has_hype(low)
    # Standing Gen1 notice is always present.
    assert "INVALIDATED" in html
    # Deployment page must NOT carry the preview-only "not deployed" language.
    assert "not deployed" not in low


def test_pages_html_shows_boundary_and_data_age(tmp_path):
    coord = _active_coord_with_tick(tmp_path)
    sb = dashboard.build_scoreboard(coord.exp_dir)
    epoch_ms = sb["decision_epoch_ms"]
    boundary = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)

    # Render 30 minutes after the boundary: fresh, no stale warning.
    html = dashboard.render_pages_html(sb, now=boundary + timedelta(minutes=30))
    assert "last successful boundary" in html.lower()
    assert "data age" in html.lower()
    assert "stale data" not in html.lower()


def test_pages_html_stale_warning_past_threshold(tmp_path):
    coord = _active_coord_with_tick(tmp_path)
    sb = dashboard.build_scoreboard(coord.exp_dir)
    epoch_ms = sb["decision_epoch_ms"]
    boundary = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)

    # Well past the stale threshold: the warning must appear.
    late = boundary + timedelta(seconds=dashboard.STALE_AFTER_S + 3600)
    html = dashboard.render_pages_html(sb, now=late)
    assert "stale data" in html.lower()
    # Still shows the disclaimer even when stale.
    assert "no real money" in html.lower()


# --------------------------------------------------------------------------
# PREPARED (no tick) renders an explicit "not trading yet", never fake numbers
# --------------------------------------------------------------------------
def test_pages_html_prepared_shows_not_trading(tmp_path):
    coord = _prepared_coord(tmp_path)
    sb = dashboard.build_scoreboard(coord.exp_dir)
    html = dashboard.render_pages_html(sb)
    low = html.lower()
    assert "no tick published yet" in low
    assert "no real money" in low
    # No stale warning when there is simply no published tick yet.
    assert "stale data" not in low


# --------------------------------------------------------------------------
# site builder writes index.html + .nojekyll
# --------------------------------------------------------------------------
def test_build_pages_site_writes_index_and_nojekyll(tmp_path):
    coord = _active_coord_with_tick(tmp_path)
    out = tmp_path / "site"
    sb = dashboard.build_pages_site(coord.exp_dir, str(out))

    index = out / "index.html"
    assert index.is_file()
    assert (out / ".nojekyll").is_file()
    html = index.read_text(encoding="utf-8")
    assert "no real money" in html.lower()
    assert sb["experiment_id"] in html


# --------------------------------------------------------------------------
# fail-closed: a corrupt CURRENT must NOT produce a page
# --------------------------------------------------------------------------
def test_build_pages_site_fails_closed_on_corrupt_current(tmp_path):
    coord = _active_coord_with_tick(tmp_path)
    # Corrupt the ONLY published-state pointer.
    current_path = os.path.join(coord.exp_dir, cp.CURRENT_NAME)
    with open(current_path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json")

    out = tmp_path / "site"
    with pytest.raises(cp.CheckpointError):
        dashboard.build_pages_site(coord.exp_dir, str(out))
    # Nothing was written (no half-built page to deploy over a good one).
    assert not (out / "index.html").exists()


# --------------------------------------------------------------------------
# placeholder page for the dormant phase (no deployable experiment)
# --------------------------------------------------------------------------
def test_placeholder_html_is_honest(tmp_path):
    html = dashboard.render_pages_placeholder_html()
    low = html.lower()
    assert "no active generation-2 experiment" in low
    assert "no real money" in low
    assert "INVALIDATED" in html
    assert not _has_hype(low)


# --------------------------------------------------------------------------
# deployable-experiment selection: retired/terminal excluded
# --------------------------------------------------------------------------
def test_deployable_selects_single_prepared(tmp_path):
    coord = _prepared_coord(tmp_path)
    state_root = str(tmp_path / "state")
    assert _deployable_experiment_id(state_root) == coord.manifest.experiment_id


def test_deployable_excludes_terminal_experiment(tmp_path):
    coord = _prepared_coord(tmp_path)
    state_root = str(tmp_path / "state")
    # Plant a second, CLOSED (terminal) experiment alongside the live one.
    other = os.path.join(state_root, "gen2", "gen2-closedexample")
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write('{"experiment_id": "gen2-closedexample", "status": "CLOSED"}')

    # The terminal one is ignored; the single deployable one is returned.
    assert _deployable_experiment_id(state_root) == coord.manifest.experiment_id


def test_deployable_none_when_all_terminal(tmp_path):
    state_root = str(tmp_path / "state")
    closed = os.path.join(state_root, "gen2", "gen2-closedexample")
    os.makedirs(closed, exist_ok=True)
    with open(os.path.join(closed, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write('{"experiment_id": "gen2-closedexample", "status": "CLOSED"}')
    assert _deployable_experiment_id(state_root) is None
