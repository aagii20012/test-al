"""Regression suite for the v2 canonical source hash + permanent refusal.

These tests exist because the Generation-2 Gate-L1 canary failed with class
``SOURCE_BINDING_PORTABILITY``: the v1 binding hashed raw working-tree bytes, so
a Windows CRLF checkout (``9681fdba…``) and the GitHub Ubuntu LF checkout
(``578dfa75…``) produced different digests and the code binding could never
re-verify on Linux. The v2 algorithm (``python-source-canonical-sha256`` v2)
normalises newlines and frames the preimage. This file locks in every property
the remediation depends on, and proves the failed experiment can never tick.

Everything here is offline and platform-independent: fixture trees are built
in-memory with explicit line endings so the CRLF/LF proof runs identically on
Windows and on the Linux CI runner.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _gen2_helpers import ANCHOR, make_coord, write_config  # noqa: E402

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import experiment as exp  # noqa: E402
from algotrading.gen2 import source_hash as sh  # noqa: E402
from algotrading.gen2.coordinator import NotActivatedError  # noqa: E402
from algotrading.gen2.experiment import Status, build_manifest  # noqa: E402


# --------------------------------------------------------------------------
# Fixture-tree builder: a minimal fake ``algotrading/`` package with a matching
# declared inventory, written with a chosen line ending.
# --------------------------------------------------------------------------
BASE_FILES = {
    "algotrading/__init__.py": "VERSION = '0.0.0'\n",
    "algotrading/gen2/__init__.py": "# gen2 package\n",
    "algotrading/gen2/experiment.py": (
        "def score(x):\n"
        "    total = 0\n"
        "    for i in range(x):\n"
        "        total += i\n"
        "    return total\n"
    ),
    "algotrading/strategy/momentum.py": "LOOKBACK = 96\nTHRESHOLD = 1.0\n",
}


def _write_file(path: str, text: str, eol: bytes) -> None:
    """Write ``text`` (given with LF) using the requested line ending, raw."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    data = norm.replace("\n", eol.decode("latin-1")).encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def build_fixture(root, files=None, *, eol=b"\n", regen_inventory=True):
    """Materialise a fake source tree + inventory under ``root``; return str root."""
    root = str(root)
    files = BASE_FILES if files is None else files
    for rel, text in files.items():
        _write_file(os.path.join(root, *rel.split("/")), text, eol)
    if regen_inventory:
        inv = sh.generate_inventory(root=root)
        sh.write_inventory(inv, os.path.join(root, *sh.INVENTORY_REL.split("/")))
    return root


def _hash(root):
    return sh.canonical_source_hash(root=str(root))["sha256"]


# ==========================================================================
# 1. Portability: LF == CRLF == mixed == lone-CR  (the actual defect fixed)
# ==========================================================================
def test_lf_crlf_mixed_and_cr_hash_identically(tmp_path):
    lf = build_fixture(tmp_path / "lf", eol=b"\n")
    crlf = build_fixture(tmp_path / "crlf", eol=b"\r\n")
    cr = build_fixture(tmp_path / "cr", eol=b"\r")  # old-Mac lone CR

    # A genuinely mixed tree: different files use different endings.
    mixed_root = str(tmp_path / "mixed")
    for i, (rel, text) in enumerate(BASE_FILES.items()):
        eol = [b"\n", b"\r\n", b"\r"][i % 3]
        _write_file(os.path.join(mixed_root, *rel.split("/")), text, eol)
    inv = sh.generate_inventory(root=mixed_root)
    sh.write_inventory(inv, os.path.join(mixed_root, *sh.INVENTORY_REL.split("/")))

    h_lf = _hash(lf)
    assert _hash(crlf) == h_lf, "CRLF (Windows) must match LF (Linux)"
    assert _hash(cr) == h_lf, "lone CR must normalise to LF"
    assert _hash(mixed_root) == h_lf, "mixed endings must match LF"


def test_windows_and_linux_style_trees_match(tmp_path):
    """Named for Message A §4: Windows-style vs Linux-style trees hash equal."""
    windows = build_fixture(tmp_path / "win", eol=b"\r\n")
    linux = build_fixture(tmp_path / "nix", eol=b"\n")
    assert _hash(windows) == _hash(linux)


def test_normalize_text_collapses_all_newline_styles():
    assert sh.normalize_text(b"a\r\nb\rc\n") == b"a\nb\nc\n"
    assert sh.normalize_text(b"a\r\n\r\nb") == b"a\n\nb"


def test_hash_is_deterministic(tmp_path):
    root = build_fixture(tmp_path / "d")
    assert _hash(root) == _hash(root)


# ==========================================================================
# 2. Sensitivity: real edits (content / whitespace / indent / path) DO change
# ==========================================================================
def test_content_change_changes_hash(tmp_path):
    base = _hash(build_fixture(tmp_path / "a"))
    files = dict(BASE_FILES)
    files["algotrading/gen2/experiment.py"] = files[
        "algotrading/gen2/experiment.py"].replace("total = 0", "total = 1")
    changed = _hash(build_fixture(tmp_path / "b", files=files))
    assert changed != base


def test_trailing_whitespace_change_changes_hash(tmp_path):
    base = _hash(build_fixture(tmp_path / "a"))
    files = dict(BASE_FILES)
    files["algotrading/__init__.py"] = "VERSION = '0.0.0' \n"  # trailing space
    changed = _hash(build_fixture(tmp_path / "b", files=files))
    assert changed != base


def test_indentation_change_changes_hash(tmp_path):
    base = _hash(build_fixture(tmp_path / "a"))
    files = dict(BASE_FILES)
    files["algotrading/gen2/experiment.py"] = files[
        "algotrading/gen2/experiment.py"].replace(
        "        total += i", "\t\ttotal += i")  # spaces -> tabs
    changed = _hash(build_fixture(tmp_path / "b", files=files))
    assert changed != base


def test_path_change_changes_hash(tmp_path):
    base = _hash(build_fixture(tmp_path / "a"))
    files = dict(BASE_FILES)
    files["algotrading/strategy/momentum2.py"] = files.pop(
        "algotrading/strategy/momentum.py")  # rename => different path
    changed = _hash(build_fixture(tmp_path / "b", files=files))
    assert changed != base


# ==========================================================================
# 3. Fail-closed: missing / extra / duplicate / invalid-utf8 / symlink / escape
# ==========================================================================
def test_missing_declared_file_fails_closed(tmp_path):
    root = build_fixture(tmp_path / "a")
    # Delete a declared file from disk; inventory still lists it -> drift.
    os.remove(os.path.join(root, "algotrading", "strategy", "momentum.py"))
    with pytest.raises(sh.SourceHashError):
        sh.canonical_source_hash(root=root)


def test_extra_undeclared_file_fails_closed(tmp_path):
    root = build_fixture(tmp_path / "a")
    _write_file(os.path.join(root, "algotrading", "sneaky.py"), "x = 1\n", b"\n")
    with pytest.raises(sh.SourceHashError):
        sh.canonical_source_hash(root=root)


def test_duplicate_declared_path_fails_closed(tmp_path):
    root = build_fixture(tmp_path / "a")
    inv_path = os.path.join(root, *sh.INVENTORY_REL.split("/"))
    inv = json.load(open(inv_path))
    inv["files"] = inv["files"] + [inv["files"][0]]  # duplicate one path
    sh.write_inventory(inv, inv_path)
    with pytest.raises(sh.SourceHashError):
        sh.canonical_source_hash(root=root)


def test_invalid_utf8_fails_closed(tmp_path):
    files = dict(BASE_FILES)
    root = build_fixture(tmp_path / "a", files=files)
    # Overwrite one declared file with invalid UTF-8 (still on disk + declared,
    # so drift passes and the failure comes from strict decode).
    bad = os.path.join(root, "algotrading", "__init__.py")
    with open(bad, "wb") as fh:
        fh.write(b"\xff\xfe not valid utf-8 \x80")
    with pytest.raises(sh.SourceHashError):
        sh.canonical_source_hash(root=root)


def test_symlink_in_tree_fails_closed(tmp_path):
    root = build_fixture(tmp_path / "a")
    target = os.path.join(root, "algotrading", "__init__.py")
    link = os.path.join(root, "algotrading", "linked.py")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation not permitted in this environment")
    # It is now an extra .py on disk (drift) AND a symlink (unsafe path); either
    # way canonical_source_hash must fail closed.
    with pytest.raises(sh.SourceHashError):
        sh.canonical_source_hash(root=root)
    # And directly, a symlinked *declared* file is rejected by _safe_full_path.
    with pytest.raises(sh.SourceHashError):
        sh._safe_full_path(root, "algotrading/linked.py")


@pytest.mark.parametrize("bad", [
    "../etc/passwd",
    "/absolute/path.py",
    "C:/win/abs.py",
    "algotrading\\gen2\\back.py",       # backslash
    "algotrading/../secret.py",          # dot-dot component
    "algotrading/./x.py",                # dot component
    " algotrading/x.py",                 # surrounding whitespace
    "algotrading/x.txt",                 # not .py
    "notalgotrading/x.py",               # wrong root
    "",                                   # empty
])
def test_path_validation_rejects_unsafe_paths(bad):
    with pytest.raises(sh.SourceHashError):
        sh._validate_rel_path(bad)


# ==========================================================================
# 4. Sorting is deterministic regardless of declared order
# ==========================================================================
def test_hash_independent_of_inventory_order(tmp_path):
    root = build_fixture(tmp_path / "a")
    inv_path = os.path.join(root, *sh.INVENTORY_REL.split("/"))
    inv = json.load(open(inv_path))
    sorted_hash = sh.canonical_source_hash(root=root)["sha256"]
    inv["files"] = list(reversed(inv["files"]))  # scramble declared order
    sh.write_inventory(inv, inv_path)
    assert sh.canonical_source_hash(root=root)["sha256"] == sorted_hash


# ==========================================================================
# 5. v1 / v2 identities cannot be confused
# ==========================================================================
def test_inventory_rejects_wrong_algorithm(tmp_path):
    root = build_fixture(tmp_path / "a")
    inv_path = os.path.join(root, *sh.INVENTORY_REL.split("/"))
    inv = json.load(open(inv_path))
    inv["algorithm"] = "raw-byte-source-tree"  # a v1-style name
    sh.write_inventory(inv, inv_path)
    with pytest.raises(sh.SourceHashError):
        sh.load_inventory(root=root)


def test_inventory_rejects_wrong_version(tmp_path):
    root = build_fixture(tmp_path / "a")
    inv_path = os.path.join(root, *sh.INVENTORY_REL.split("/"))
    inv = json.load(open(inv_path))
    inv["version"] = 1
    sh.write_inventory(inv, inv_path)
    with pytest.raises(sh.SourceHashError):
        sh.load_inventory(root=root)


def test_algorithm_and_version_are_folded_into_preimage():
    """A raw concatenation digest (v1-style) must differ from the framed v2 one.

    Proves the algorithm name + version + framing are part of the preimage, so a
    v1 digest can never coincide with a v2 digest over the same file contents.
    """
    import hashlib
    entries = [(b"algotrading/a.py", b"x = 1\n"),
               (b"algotrading/b.py", b"y = 2\n")]
    v2 = sh._digest(entries)
    # crude v1-like digest: bare concatenation, no name/version/length framing
    raw = hashlib.sha256(b"".join(p + c for p, c in entries)).hexdigest()
    assert v2 != raw


# ==========================================================================
# 6. Manifest-level: a v1-bound manifest cannot verify against running v2 code
# ==========================================================================
def _real_manifest(tmp_path):
    cfg = write_config(tmp_path / "cfg.yaml")
    return build_manifest(created=ANCHOR, config_path=cfg)


def test_v2_manifest_verifies_against_real_tree(tmp_path):
    """Sanity: an honest v2 build_manifest verifies against the real repo tree."""
    m = _real_manifest(tmp_path)
    coord = make_coord(m, tmp_path / "state", tmp_path / "cfg.yaml",
                       fetch=None, verify_code=True)
    coord._verify_code_binding()  # must not raise
    # The manifest actually recorded the v2 identity.
    assert m.code["source_hash_algorithm"] == sh.ALGORITHM
    assert m.code["source_hash_version"] == sh.VERSION
    assert "source_inventory_sha256" in m.code


def test_v1_bound_manifest_refused(tmp_path):
    """A manifest whose code binding predates v2 (no algorithm marker, a raw
    v1 sha) must be refused by _verify_code_binding — algorithm identity first."""
    m = _real_manifest(tmp_path)
    m.code = {
        "implementation_commit": "9b751bfc1b9c4d3f94b4527eebc57a7fb028e17a",
        "source_file_count": 57,
        "source_tree_sha256":
            "9681fdbadfa076a8ebfe64af33fe9444c3967a582854e65cbfddd01850793d41",
    }
    m.binding_sha256 = m.recompute_binding()  # binding self-consistent again
    coord = make_coord(m, tmp_path / "state", tmp_path / "cfg.yaml",
                       fetch=None, verify_code=True)
    with pytest.raises(state_schema.IncompatibleStateError):
        coord._verify_code_binding()


def test_v2_inventory_mismatch_refused(tmp_path):
    """Same algorithm+version+tree hash but a different declared inventory hash
    must still be refused (the declared file set changed)."""
    m = _real_manifest(tmp_path)
    code = dict(m.code)
    code["source_inventory_sha256"] = "0" * 64
    m.code = code
    m.binding_sha256 = m.recompute_binding()
    coord = make_coord(m, tmp_path / "state", tmp_path / "cfg.yaml",
                       fetch=None, verify_code=True)
    with pytest.raises(state_schema.IncompatibleStateError):
        coord._verify_code_binding()


# ==========================================================================
# 7. The failed experiment permanently refuses every tick + reactivation
# ==========================================================================
RETIRED_ID = "gen2-20260724T023914Z-b91b8e74"


def test_retired_id_is_registered():
    assert RETIRED_ID in exp.RETIRED_EXPERIMENT_IDS


def _prepared_coord_with_id(tmp_path, experiment_id, status=Status.PREPARED):
    cfg = write_config(tmp_path / "cfg.yaml")
    m = build_manifest(created=ANCHOR, config_path=cfg, status=status)
    m.experiment_id = experiment_id
    m.binding_sha256 = m.recompute_binding()
    coord = make_coord(m, tmp_path / "state", cfg, fetch=None, verify_code=False)
    coord.prepare()
    return coord


def test_retired_experiment_refuses_dry_tick(tmp_path):
    coord = _prepared_coord_with_id(tmp_path, RETIRED_ID)
    with pytest.raises(NotActivatedError):
        coord.run_tick(dry_run=True, now=ANCHOR)


def test_retired_experiment_refuses_live_tick(tmp_path):
    coord = _prepared_coord_with_id(tmp_path, RETIRED_ID)
    with pytest.raises(NotActivatedError):
        coord.run_tick(dry_run=False, now=ANCHOR)


def test_retired_experiment_cannot_be_reactivated(tmp_path):
    coord = _prepared_coord_with_id(tmp_path, RETIRED_ID)
    with pytest.raises(NotActivatedError):
        coord.set_status(Status.ACTIVE, approved=True)


def test_terminal_status_refuses_ticks(tmp_path):
    """A non-retired id in a TERMINAL status (FAILED_CANARY) also refuses ticks."""
    fresh_id = "gen2-20990101T000000Z-deadbeef"
    assert fresh_id not in exp.RETIRED_EXPERIMENT_IDS
    coord = _prepared_coord_with_id(tmp_path, fresh_id, status=Status.FAILED_CANARY)
    with pytest.raises(NotActivatedError):
        coord.run_tick(dry_run=True, now=ANCHOR)
    with pytest.raises(NotActivatedError):
        coord.run_tick(dry_run=False, now=ANCHOR)


def test_failed_canary_is_terminal():
    assert Status.FAILED_CANARY in Status.TERMINAL
    assert Status.CLOSED in Status.TERMINAL
    assert Status.ACTIVE not in Status.TERMINAL
