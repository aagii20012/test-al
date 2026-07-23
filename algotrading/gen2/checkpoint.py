"""Immutable, content-addressed checkpoint store for a Generation-2 experiment.

A published Generation-2 experiment is a directory of *immutable* checkpoints
plus ONE pointer file that names the single published checkpoint:

    state/gen2/<experiment_id>/
        manifest.json                       # the immutable experiment binding
        CURRENT                             # the ONLY published-state pointer
        checkpoints/
            <boundary_epoch>-<snapshot_sha256>/     # one immutable checkpoint
                bots/<bot_id>.json
                audit/tick.json
                run_status.json
                market_snapshot.json
                CHECKPOINT_MANIFEST.json    # sha256 of every artifact above

Publication is a two-phase, crash-safe protocol (see ``coordinator.py``):

  1. Every artifact is computed into a NEW ``.staging-*`` directory, each file is
     hashed into ``CHECKPOINT_MANIFEST.json``, everything is fsync'd, and the
     staging directory is atomically renamed to its immutable final name
     ``<boundary_epoch>-<snapshot_sha256>``. A checkpoint directory therefore
     only ever exists complete — a rename is atomic.
  2. The single ``CURRENT`` pointer is replaced LAST (atomic ``os.replace``).
     ``CURRENT`` carries ``checkpoint_manifest_sha256`` so a reader can prove the
     on-disk manifest is exactly the one that was published.

Only the checkpoint named by ``CURRENT`` is "published". Every reader — the
coordinator loading prior state, and the dashboard — resolves ``CURRENT``,
verifies the manifest hash and every artifact hash, and reads ONLY that
checkpoint. Nothing ever scans for a "latest" file. An orphan checkpoint left by
a crash between the rename and the ``CURRENT`` flip is invisible to readers and
is only ever reused if it matches the intended publication EXACTLY; otherwise the
store fails closed (it is never silently deleted).

This module is the single shared definition of the on-disk format and its
verification. It imports only hashing helpers + the snapshot loader — no network,
no exchange, no order endpoint (see the no-order-endpoints test).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .. import state_schema
from .experiment import sha256_bytes
from .snapshot import MarketSnapshot, SnapshotError, load_snapshot

# ---- on-disk names (the single source of truth for both writer and readers) --
CHECKPOINTS_DIRNAME = "checkpoints"
CURRENT_NAME = "CURRENT"
STAGING_PREFIX = ".staging-"

CHECKPOINT_MANIFEST_NAME = "CHECKPOINT_MANIFEST.json"
RUN_STATUS_NAME = "run_status.json"
SNAPSHOT_NAME = "market_snapshot.json"
BOTS_DIRNAME = "bots"
AUDIT_DIRNAME = "audit"


class CheckpointError(state_schema.IncompatibleStateError):
    """A checkpoint / CURRENT pointer failed integrity or identity verification.

    Subclasses ``IncompatibleStateError`` so the coordinator's blanket
    fail-closed handling (and every existing test asserting that type) treats a
    corrupt checkpoint exactly like any other unloadable state: refuse, never
    repair, never silently proceed.
    """


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------
def checkpoints_dir(exp_dir: str) -> str:
    return os.path.join(exp_dir, CHECKPOINTS_DIRNAME)


def current_path(exp_dir: str) -> str:
    return os.path.join(exp_dir, CURRENT_NAME)


def checkpoint_dir(exp_dir: str, name: str) -> str:
    return os.path.join(checkpoints_dir(exp_dir), name)


def staging_dir(exp_dir: str, name: str) -> str:
    return os.path.join(checkpoints_dir(exp_dir), STAGING_PREFIX + name)


def checkpoint_name(boundary_epoch_ms: int, snapshot_sha256: str) -> str:
    """Content-addressed name: ``<boundary_epoch_ms>-<snapshot_sha256>``.

    Two different boundaries, or the same boundary over different market bytes,
    are necessarily different checkpoints — the name pins both.
    """
    return f"{int(boundary_epoch_ms)}-{snapshot_sha256}"


def list_checkpoints(exp_dir: str) -> List[str]:
    """Every materialised checkpoint directory name (ignores ``.staging-*``)."""
    root = checkpoints_dir(exp_dir)
    if not os.path.isdir(root):
        return []
    return sorted(
        n for n in os.listdir(root)
        if os.path.isdir(os.path.join(root, n)) and not n.startswith("."))


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------
@dataclass
class CurrentRef:
    """The parsed, structurally-validated ``CURRENT`` pointer."""

    experiment_id: str
    checkpoint: str
    checkpoint_manifest_sha256: str
    boundary_epoch: int
    snapshot_sha256: str
    prior_checkpoint: Optional[str]
    dry_run: bool
    published_utc: str

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "checkpoint": self.checkpoint,
            "checkpoint_manifest_sha256": self.checkpoint_manifest_sha256,
            "boundary_epoch": self.boundary_epoch,
            "snapshot_sha256": self.snapshot_sha256,
            "prior_checkpoint": self.prior_checkpoint,
            "dry_run": self.dry_run,
            "published_utc": self.published_utc,
        }


@dataclass
class Checkpoint:
    """A fully-verified, in-memory view of one immutable checkpoint."""

    name: str
    dir: str
    manifest: dict
    run_status: dict
    audit: dict
    snapshot: MarketSnapshot
    bot_states: Dict[str, dict] = field(default_factory=dict)
    checkpoint_manifest_sha256: str = ""

    @property
    def boundary_epoch(self) -> int:
        return int(self.manifest["boundary_epoch"])

    @property
    def dry_run(self) -> bool:
        return bool(self.manifest.get("dry_run", False))


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------
def hash_dir(root: str) -> Dict[str, str]:
    """``relpath -> sha256`` for every file under ``root`` EXCEPT the manifest.

    The manifest is excluded because it *contains* these hashes; its own bytes
    are covered separately by ``CURRENT.checkpoint_manifest_sha256``.
    """
    out: Dict[str, str] = {}
    for base, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(base, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel == CHECKPOINT_MANIFEST_NAME:
                continue
            with open(full, "rb") as fh:
                out[rel] = sha256_bytes(fh.read())
    return out


# --------------------------------------------------------------------------
# reading + verifying
# --------------------------------------------------------------------------
def read_current(exp_dir: str, *, experiment_id: Optional[str] = None
                 ) -> Optional[CurrentRef]:
    """Parse ``CURRENT``. ``None`` only if it is genuinely absent; else fail closed.

    A present-but-corrupt or foreign pointer raises ``CheckpointError`` — it is
    never treated as "no state" (that would silently un-publish an experiment).
    """
    path = current_path(exp_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        d = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        raise CheckpointError(
            f"CURRENT pointer at {path!r} is unreadable/corrupt: {e}. Refusing to "
            "treat a broken pointer as 'no published state'.")
    if not isinstance(d, dict):
        raise CheckpointError(f"CURRENT pointer at {path!r} is not a JSON object.")
    required = ("checkpoint", "checkpoint_manifest_sha256", "boundary_epoch",
                "snapshot_sha256", "experiment_id")
    for k in required:
        if k not in d:
            raise CheckpointError(
                f"CURRENT pointer at {path!r} is missing required field {k!r}.")
    if experiment_id is not None and d["experiment_id"] != experiment_id:
        raise CheckpointError(
            f"CURRENT pointer at {path!r} names experiment "
            f"{d['experiment_id']!r}, not {experiment_id!r}.")
    return CurrentRef(
        experiment_id=d["experiment_id"],
        checkpoint=d["checkpoint"],
        checkpoint_manifest_sha256=d["checkpoint_manifest_sha256"],
        boundary_epoch=int(d["boundary_epoch"]),
        snapshot_sha256=d["snapshot_sha256"],
        prior_checkpoint=d.get("prior_checkpoint"),
        dry_run=bool(d.get("dry_run", False)),
        published_utc=d.get("published_utc", ""))


def validate_checkpoint(exp_dir: str, name: str, *, experiment_id: str,
                        expected: Optional[dict] = None) -> str:
    """Verify a checkpoint end-to-end and return its ``CHECKPOINT_MANIFEST`` sha256.

    Checks, all fail-closed (``CheckpointError``):
      * the checkpoint manifest exists and parses;
      * generation / schema / experiment-id match this experiment;
      * the directory name equals ``<boundary_epoch>-<snapshot_sha256>`` from the
        manifest (content-address integrity — a renamed dir is rejected);
      * every artifact hash in the manifest matches the file on disk, and there
        are no missing or extra artifacts;
      * the embedded market snapshot's own hash matches ``snapshot_sha256``;
      * every key in ``expected`` matches the manifest exactly (used for orphan
        reuse — a non-matching orphan is refused, never deleted).
    """
    cdir = checkpoint_dir(exp_dir, name)
    mpath = os.path.join(cdir, CHECKPOINT_MANIFEST_NAME)
    if not os.path.isfile(mpath):
        raise CheckpointError(
            f"checkpoint {name!r} has no {CHECKPOINT_MANIFEST_NAME} "
            f"(directory missing or incomplete at {cdir!r}).")
    with open(mpath, "rb") as fh:
        raw = fh.read()
    cp_manifest_sha = sha256_bytes(raw)
    try:
        cpm = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CheckpointError(f"checkpoint {name!r} manifest is not valid JSON: {e}")

    if cpm.get("experiment_id") != experiment_id:
        raise CheckpointError(
            f"checkpoint {name!r} belongs to experiment "
            f"{cpm.get('experiment_id')!r}, not {experiment_id!r}.")
    if cpm.get("generation") != state_schema.GENERATION:
        raise CheckpointError(
            f"checkpoint {name!r} is generation {cpm.get('generation')!r}, "
            f"expected {state_schema.GENERATION!r}.")
    if cpm.get("schema_version") != state_schema.SCHEMA_VERSION:
        raise CheckpointError(
            f"checkpoint {name!r} is schema_version {cpm.get('schema_version')!r}, "
            f"expected {state_schema.SCHEMA_VERSION}.")
    if cpm.get("checkpoint") != name:
        raise CheckpointError(
            f"checkpoint {name!r} manifest self-names {cpm.get('checkpoint')!r}.")
    expect_name = checkpoint_name(cpm.get("boundary_epoch"), cpm.get("snapshot_sha256"))
    if expect_name != name:
        raise CheckpointError(
            f"checkpoint {name!r} is not content-addressed: manifest boundary + "
            f"snapshot hash address {expect_name!r}.")

    listed = cpm.get("artifacts")
    if not isinstance(listed, dict) or not listed:
        raise CheckpointError(f"checkpoint {name!r} manifest lists no artifacts.")
    actual = hash_dir(cdir)
    if set(listed) != set(actual):
        missing = sorted(set(listed) - set(actual))
        extra = sorted(set(actual) - set(listed))
        raise CheckpointError(
            f"checkpoint {name!r} artifact set differs from its manifest "
            f"(missing={missing}, extra={extra}).")
    for rel, want in listed.items():
        if actual[rel] != want:
            raise CheckpointError(
                f"checkpoint {name!r} artifact {rel!r} is corrupted: sha256 "
                f"{actual[rel]!r} != manifest {want!r}.")

    # The embedded snapshot must verify its OWN hash and match the address.
    try:
        snap = load_snapshot(os.path.join(cdir, SNAPSHOT_NAME))
    except SnapshotError as e:
        raise CheckpointError(f"checkpoint {name!r} snapshot is invalid: {e}")
    if snap.sha256 != cpm.get("snapshot_sha256"):
        raise CheckpointError(
            f"checkpoint {name!r} snapshot hash {snap.sha256!r} disagrees with "
            f"manifest {cpm.get('snapshot_sha256')!r}.")

    if expected:
        for k, v in expected.items():
            if cpm.get(k) != v:
                raise CheckpointError(
                    f"checkpoint {name!r} does not match the intended publication "
                    f"({k}={cpm.get(k)!r} != expected {v!r}); refusing to reuse an "
                    "orphan that is not exactly this tick. (Not deleted — "
                    "investigate manually.)")
    return cp_manifest_sha


def load_checkpoint(exp_dir: str, name: str, *, experiment_id: str,
                    expected_manifest_sha: Optional[str] = None,
                    expected: Optional[dict] = None) -> Checkpoint:
    """Verify then read a checkpoint into memory (bot states parsed as JSON)."""
    cp_manifest_sha = validate_checkpoint(
        exp_dir, name, experiment_id=experiment_id, expected=expected)
    if (expected_manifest_sha is not None
            and cp_manifest_sha != expected_manifest_sha):
        raise CheckpointError(
            f"checkpoint {name!r} manifest hash {cp_manifest_sha!r} does not match "
            f"the CURRENT pointer's {expected_manifest_sha!r}; the pointer and the "
            "checkpoint disagree.")

    cdir = checkpoint_dir(exp_dir, name)
    with open(os.path.join(cdir, CHECKPOINT_MANIFEST_NAME), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(os.path.join(cdir, RUN_STATUS_NAME), "r", encoding="utf-8") as fh:
        run_status = json.load(fh)
    audit_path = os.path.join(cdir, AUDIT_DIRNAME, "tick.json")
    audit = {}
    if os.path.isfile(audit_path):
        with open(audit_path, "r", encoding="utf-8") as fh:
            audit = json.load(fh)
    snapshot = load_snapshot(os.path.join(cdir, SNAPSHOT_NAME))

    bot_states: Dict[str, dict] = {}
    bots_dir = os.path.join(cdir, BOTS_DIRNAME)
    if os.path.isdir(bots_dir):
        for fname in sorted(os.listdir(bots_dir)):
            if not fname.endswith(".json"):
                continue
            bot_id = fname[:-len(".json")]
            bpath = os.path.join(bots_dir, fname)
            try:
                with open(bpath, "r", encoding="utf-8") as fh:
                    bot_states[bot_id] = json.load(fh)
            except json.JSONDecodeError as e:
                # Hash-consistent but not JSON: refuse (never fresh-start silently).
                raise CheckpointError(
                    f"bot state {bpath!r} in checkpoint {name!r} is not valid "
                    f"JSON: {e}. Refusing to resume corrupted state.")
    return Checkpoint(
        name=name, dir=cdir, manifest=manifest, run_status=run_status,
        audit=audit, snapshot=snapshot, bot_states=bot_states,
        checkpoint_manifest_sha256=cp_manifest_sha)


def resolve_current(exp_dir: str, *, experiment_id: Optional[str] = None
                    ) -> Optional[Checkpoint]:
    """Resolve ``CURRENT`` to a fully-verified checkpoint (the ONLY published one).

    This is the sole entry point a reader (the dashboard) should use: it reads the
    pointer, then verifies the manifest-hash + every artifact hash of exactly the
    named checkpoint. Returns ``None`` if the experiment has never published.
    """
    cur = read_current(exp_dir, experiment_id=experiment_id)
    if cur is None:
        return None
    exp_id = experiment_id or cur.experiment_id
    cp = load_checkpoint(
        exp_dir, cur.checkpoint, experiment_id=exp_id,
        expected_manifest_sha=cur.checkpoint_manifest_sha256)
    if (int(cp.manifest["boundary_epoch"]) != cur.boundary_epoch
            or cp.manifest.get("snapshot_sha256") != cur.snapshot_sha256):
        raise CheckpointError(
            f"CURRENT metadata disagrees with checkpoint {cur.checkpoint!r} "
            "(boundary/snapshot mismatch).")
    return cp
