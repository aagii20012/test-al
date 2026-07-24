#!/usr/bin/env python3
"""Generation-2 manual control-plane helpers — OUTSIDE the bound source tree.

This module lives under ``tools/`` on purpose. The Generation-2 code binding is a
canonical hash of the ``algotrading/`` package ONLY (see
``algotrading/gen2/source_hash.py`` + ``source_inventory.json``). Anything added
here therefore CANNOT change ``source_tree_sha256`` and so can never invalidate a
PREPARED experiment's immutable binding — the exact experiment the operator is
about to activate stays activatable byte-for-byte.

It exposes the things the GitHub manual-control workflow
(``.github/workflows/gen2-control.yml``) needs that the engine CLI does not
already provide:

  guard           READ-ONLY gatekeeper. Asserts that ``--experiment-id`` names a
                  committed, non-retired, non-terminal experiment whose immutable
                  binding still verifies, AND that the requested ``--action`` is
                  legal for its current status. Mutates nothing; exits non-zero
                  (fail closed) on any violation. The workflow runs this first, in
                  a read-only job, before it is ever granted write permission.

  pause           ACTIVE -> PAUSED via the coordinator's own audited
                  ``set_status``. This is the only lifecycle transition the engine
                  CLI omits. It refuses a retired id or any status other than
                  ACTIVE, so it can only ever halt a genuinely live experiment
                  (never resurrect or mislabel one).

  replay-current  READ-ONLY, DETERMINISTIC idempotency proof. Re-runs the ONE
                  boundary that ``CURRENT`` already published, feeding the
                  coordinator's normal tick path the *stored* immutable market
                  snapshot (never the live window, so market movement can never
                  select a newer candle). It requires the result
                  ``ALREADY_PUBLISHED`` and then asserts byte-for-byte identity of
                  the whole experiment directory before/after — CURRENT, the
                  checkpoint manifest, all bot states, the audit record, the
                  checkpoint directory listing, plus the fill/fee/equity ledger.
                  It fails closed if ANYTHING changed or if it did not land on the
                  exact published boundary. This is the safe replacement for
                  "dispatch canary twice and hope no new candle appeared in
                  between": it can only ever confirm the published boundary is a
                  fixed point, and can never advance state.

The consequential actions themselves (activate / canary-tick / verify-current)
are NOT reimplemented here: the workflow calls the existing, already-tested
``python -m algotrading.gen2`` subcommands for those, so activation still runs the
strict keyless Coinbase provenance gate + the source-binding check inside the
engine. This file only adds the guard, the missing pause transition, and the
deterministic replay-current check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# --- import bootstrap ---------------------------------------------------------
# Allow `python tools/gen2_control.py ...` from the repo root: put the repo root
# (the parent of tools/) on sys.path so `import algotrading` resolves regardless
# of the current working directory or how the script was launched.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from algotrading import state_schema  # noqa: E402
from algotrading.gen2 import checkpoint as cp  # noqa: E402
from algotrading.gen2.coordinator import Gen2Coordinator, Gen2Error  # noqa: E402
from algotrading.gen2.experiment import (  # noqa: E402
    RETIRED_EXPERIMENT_IDS, ExperimentManifest, Status)
from algotrading.gen2.snapshot import SnapshotError, SnapshotExchange  # noqa: E402

_DEFAULT_STATE_ROOT = "state"
_DEFAULT_CONFIG = "config/config.ci.yaml"

# The tightly restricted action vocabulary the manual-control workflow may ask
# for, mapped to the ONLY experiment statuses under which each is legal.
#   activate       : PREPARED -> ACTIVE       (launch switch; engine runs provenance)
#   canary         : one live tick            (requires an already-ACTIVE experiment)
#   replay-current : re-run the published      (idempotency proof; needs an ACTIVE
#                    boundary, read-only        experiment with a published CURRENT)
#   pause          : ACTIVE   -> PAUSED       (halt a live experiment)
#   verify         : read-only integrity check (any non-terminal, non-retired status)
ALLOWED_STATUS = {
    "activate": frozenset({Status.PREPARED}),
    "canary": frozenset({Status.ACTIVE}),
    "replay-current": frozenset({Status.ACTIVE}),
    "pause": frozenset({Status.ACTIVE}),
    "verify": frozenset({Status.PREPARED, Status.ACTIVE, Status.PAUSED,
                         Status.FAILED}),
}
ACTIONS = tuple(ALLOWED_STATUS)


class ControlRefused(SystemExit):
    """Fail-closed refusal. Carries exit code 2 and a machine-greppable reason."""

    def __init__(self, reason: str):
        super().__init__(2)
        self.reason = reason


def _manifest_path(state_root: str, experiment_id: str) -> str:
    return os.path.join(state_root, state_schema.GENERATION, experiment_id,
                        "manifest.json")


def _load_manifest(state_root: str, experiment_id: str) -> ExperimentManifest:
    """Load + tamper-verify the committed manifest for exactly this id.

    Raises ControlRefused (fail closed) if the experiment is not committed, the
    id does not match, or the immutable binding has been altered.
    """
    path = _manifest_path(state_root, experiment_id)
    if not os.path.exists(path):
        raise ControlRefused(
            f"no committed experiment {experiment_id!r} (missing {path!r}); "
            "refusing to act on an unknown experiment id.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = ExperimentManifest.from_dict(json.load(fh))
        # Tamper check: the immutable definition must hash to its recorded binding.
        manifest.verify_binding()
    except (json.JSONDecodeError, KeyError, TypeError,
            state_schema.IncompatibleStateError) as e:
        # A corrupt, incomplete, or tampered manifest is never actionable.
        raise ControlRefused(
            f"manifest for {experiment_id!r} at {path!r} is unreadable or its "
            f"immutable binding does not verify ({type(e).__name__}: {e}); "
            "refusing.")
    if manifest.experiment_id != experiment_id:
        raise ControlRefused(
            f"on-disk manifest id {manifest.experiment_id!r} != requested "
            f"{experiment_id!r}; refusing (path/id mismatch).")
    return manifest


def guard(action: str, state_root: str, experiment_id: str) -> ExperimentManifest:
    """Read-only legality gate. Returns the verified manifest or raises."""
    if action not in ALLOWED_STATUS:
        raise ControlRefused(
            f"unknown action {action!r}; allowed: {', '.join(ACTIONS)}.")
    manifest = _load_manifest(state_root, experiment_id)

    # A permanently-retired experiment refuses EVERY action (defence in depth over
    # the terminal-status check below: even a manifest edited back to ACTIVE loses).
    if experiment_id in RETIRED_EXPERIMENT_IDS:
        raise ControlRefused(
            f"experiment {experiment_id!r} is permanently retired and can never "
            "be activated, ticked, or paused; mint a fresh experiment id.")
    if manifest.status in Status.TERMINAL:
        raise ControlRefused(
            f"experiment {experiment_id!r} status {manifest.status!r} is terminal; "
            "no control action is permitted.")

    allowed = ALLOWED_STATUS[action]
    if manifest.status not in allowed:
        raise ControlRefused(
            f"action {action!r} requires status in "
            f"{{{', '.join(sorted(allowed))}}}, but {experiment_id!r} is "
            f"{manifest.status!r}; refusing.")
    return manifest


def cmd_guard(args) -> int:
    manifest = guard(args.action, args.state_root, args.experiment_id)
    print(f"GEN2_CONTROL_GUARD_OK action={args.action} "
          f"experiment={manifest.experiment_id} status={manifest.status}")
    return 0


def cmd_pause(args) -> int:
    # Enforce the exact ACTIVE-only, non-retired precondition before mutating —
    # the coordinator's set_status(PAUSED) is deliberately unconditional, so the
    # legality gate lives here (and in the guard job the workflow runs first).
    manifest = guard("pause", args.state_root, args.experiment_id)
    coord = Gen2Coordinator(
        manifest, state_root=args.state_root, config_path=args.config)
    # PAUSED is not ACTIVE, so this does NOT re-run the code-binding check (halting
    # must not be blocked by drift) and needs no approval flag.
    coord.set_status(Status.PAUSED)
    print(f"GEN2_CONTROL_PAUSED experiment={coord.manifest.experiment_id} "
          f"status={coord.manifest.status}")
    return 0


# --------------------------------------------------------------------------
# replay-current — a DETERMINISTIC, read-only idempotency proof.
#
# The unsafe operator step it replaces was "dispatch canary a second time and
# expect ALREADY_PUBLISHED": if a brand-new common hourly candle appeared between
# the two dispatches, the second canary would publish that NEW boundary — it would
# NOT prove replay idempotency of the boundary already authorised. replay-current
# removes that race by construction: it never looks at the live market window. It
# re-feeds the coordinator the *stored* immutable snapshot for the boundary
# CURRENT already published, so the only non-error outcome the engine can reach is
# the ALREADY_PUBLISHED no-op. It then proves nothing moved on disk.
# --------------------------------------------------------------------------
def _dir_fingerprint(root: str) -> dict:
    """``relpath -> sha256`` for EVERY file under ``root`` (nothing excluded).

    Nothing is filtered — a stray ``.lock``, ``.tmp``, or ``.staging-*`` file that
    appeared is itself a change we must catch. Comparing two fingerprints proves
    both file *contents* and the directory *listing* are byte-identical.
    """
    out: dict = {}
    for base, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(base, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _ledger(checkpoint: "cp.Checkpoint") -> dict:
    """Per-bot fill count / fees / equity read from the verified checkpoint.

    A human-readable, semantic cross-check layered on top of the raw byte
    fingerprint: even if two different byte layouts somehow hashed identically,
    the published trading ledger (fills, fees, equity) must be unchanged too.
    """
    out: dict = {}
    for bot_id in sorted(checkpoint.bot_states):
        st = checkpoint.bot_states[bot_id]
        pf = st.get("portfolio", {}) if isinstance(st, dict) else {}
        fills = pf.get("fills", [])
        out[bot_id] = {
            "fill_seq": pf.get("fill_seq"),
            "fill_count": len(fills) if isinstance(fills, list) else None,
            "fees": pf.get("total_commission"),
            "realized_pnl": pf.get("realized_pnl"),
            "equity": st.get("equity_now") if isinstance(st, dict) else None,
        }
    return out


def replay_current(state_root: str, experiment_id: str, config: str, *,
                   verify_code: bool = True) -> dict:
    """Prove the published boundary is a fixed point. Read-only; fail closed.

    Contract (every step refuses, exit 2, rather than mutate anything):
      * requires the exact ``experiment_id`` and status ACTIVE (via ``guard``);
      * fully resolves + hash-verifies CURRENT and its checkpoint;
      * replays ONLY the stored boundary using the stored immutable snapshot, so a
        moved market / changed Coinbase window can never select another candle;
      * drives the coordinator's normal idempotency path and requires
        ALREADY_PUBLISHED on the exact published boundary / snapshot / idem key;
      * asserts the WHOLE experiment directory is byte-identical before/after
        (CURRENT, checkpoint manifest, every bot state, audit, run_status, the
        snapshot, and the checkpoint directory listing) plus the fill/fee/equity
        ledger — refusing if any single byte, file, or ledger value changed.
    """
    # 1+2. Exact id, committed, non-retired, non-terminal, ACTIVE. Read-only.
    manifest = guard("replay-current", state_root, experiment_id)

    # A reader coordinator (no market fetch) resolves + fully verifies CURRENT.
    reader = Gen2Coordinator(manifest, state_root=state_root,
                             config_path=config, verify_code=verify_code)
    try:
        current = reader.read_current()
    except state_schema.IncompatibleStateError as e:
        raise ControlRefused(
            f"CURRENT pointer for {experiment_id!r} is corrupt "
            f"({type(e).__name__}: {e}); refusing to replay a broken boundary.")
    if current is None:
        raise ControlRefused(
            f"replay-current requires a published CURRENT for {experiment_id!r}, "
            "but none exists (the experiment has never ticked). Run one canary "
            "first, then replay it.")
    try:
        # 3. Full end-to-end verification of the checkpoint (manifest hash + every
        #    artifact hash + content-address + embedded-snapshot hash).
        stored = reader.resolve_current()
    except state_schema.IncompatibleStateError as e:
        raise ControlRefused(
            f"the checkpoint CURRENT names for {experiment_id!r} failed integrity "
            f"verification ({type(e).__name__}: {e}); refusing to replay corrupt "
            "state (not repaired — investigate manually).")
    if stored is None:                       # current != None already proven
        raise ControlRefused(
            f"CURRENT for {experiment_id!r} vanished during verification; refusing.")

    # 4. The exact published identity we must reproduce and must not exceed.
    boundary = current.boundary_epoch
    snapshot_sha = current.snapshot_sha256
    stored_idem = stored.run_status.get("idempotency_key")
    idem_key = f"{experiment_id}:{boundary}"
    # The stored snapshot must itself pin exactly this boundary/hash (defence in
    # depth over resolve_current's own checkpoint<->CURRENT cross-check).
    if (stored.snapshot.shared_candle_epoch_ms != boundary
            or stored.snapshot.sha256 != snapshot_sha):
        raise ControlRefused(
            f"stored snapshot for {experiment_id!r} pins boundary "
            f"{stored.snapshot.shared_candle_epoch_ms}/{stored.snapshot.sha256!r} "
            f"but CURRENT says {boundary}/{snapshot_sha!r}; refusing.")

    # Full-directory + ledger snapshot BEFORE the replay.
    exp_dir = reader.exp_dir
    before = _dir_fingerprint(exp_dir)
    names_before = cp.list_checkpoints(exp_dir)
    ledger_before = _ledger(stored)

    # 5+6+7. Replay through the coordinator's NORMAL path, but sourced ONLY from
    # the stored immutable snapshot. build_snapshot over the frozen candles
    # reproduces a byte-identical snapshot (same boundary, same hash), so the
    # idempotency branch is the only non-error outcome. The live market is never
    # consulted, so a newer boundary can never be fetched or processed.
    replay = Gen2Coordinator(
        manifest, state_root=state_root, config_path=config,
        fetch_ohlcv=SnapshotExchange(stored.snapshot).fetch_ohlcv,
        verify_code=verify_code)
    try:
        result = replay.run_tick(dry_run=False)
    except (state_schema.IncompatibleStateError, SnapshotError, Gen2Error) as e:
        # Any fail-closed engine error (corrupt state, code drift, snapshot
        # divergence, stale/overlapping run, aborted bot) => refuse, never mutate.
        raise ControlRefused(
            f"replay of boundary {boundary} for {experiment_id!r} raised "
            f"{type(e).__name__}: {e}; refusing (a clean replay must be an "
            "ALREADY_PUBLISHED no-op).")

    # Full-directory + ledger snapshot AFTER the replay.
    after = _dir_fingerprint(exp_dir)
    names_after = cp.list_checkpoints(exp_dir)
    try:
        ledger_after = _ledger(reader.resolve_current())
    except state_schema.IncompatibleStateError as e:
        raise ControlRefused(
            f"CURRENT for {experiment_id!r} no longer verifies AFTER the replay "
            f"({type(e).__name__}: {e}); the replay must never disturb state.")

    # 8. It must be the idempotent no-op on EXACTLY the published boundary.
    if result.status != "ALREADY_PUBLISHED":
        raise ControlRefused(
            f"replay returned {result.status!r}, not ALREADY_PUBLISHED; a new "
            f"boundary may have been published. Refusing (published boundary "
            f"{boundary} was expected to be a fixed point).")
    if result.decision_epoch_ms != boundary:
        raise ControlRefused(
            f"replay processed boundary {result.decision_epoch_ms}, not the "
            f"published {boundary}; refusing (it must replay the EXACT boundary, "
            "never the latest available one).")
    if result.snapshot_sha256 != snapshot_sha:
        raise ControlRefused(
            f"replay snapshot hash {result.snapshot_sha256!r} != published "
            f"{snapshot_sha!r}; refusing.")
    if result.idempotency_key != idem_key or (
            stored_idem is not None and result.idempotency_key != stored_idem):
        raise ControlRefused(
            f"replay idempotency key {result.idempotency_key!r} != expected "
            f"{idem_key!r} (stored {stored_idem!r}); refusing.")
    if result.checkpoint != current.checkpoint:
        raise ControlRefused(
            f"replay resolved checkpoint {result.checkpoint!r} != published "
            f"{current.checkpoint!r}; refusing.")

    # 9+10+11. Nothing on disk may have changed — content OR listing.
    if names_after != names_before:
        raise ControlRefused(
            f"the checkpoint directory listing changed across the replay "
            f"({names_before} -> {names_after}); refusing (a replay must create "
            "no checkpoint).")
    changed = sorted(
        set(before) ^ set(after)
        | {p for p in (set(before) & set(after)) if before[p] != after[p]})
    if changed:
        raise ControlRefused(
            f"the replay changed on-disk files under {exp_dir!r}: {changed}. A "
            "replay must be perfectly inert (no state commit, no dashboard "
            "change); refusing.")
    if ledger_before != ledger_after:
        raise ControlRefused(
            "the published fill/fee/equity ledger changed across the replay; "
            "refusing (byte fingerprint matched but the semantic ledger did not — "
            "investigate manually).")
    # A stray run lock would have shown up in `changed`; assert explicitly too.
    if os.path.exists(reader.lock_path):
        raise ControlRefused(
            f"a run lock lingered at {reader.lock_path!r} after the replay; "
            "refusing.")

    return {
        "status": result.status,
        "experiment_id": experiment_id,
        "boundary_epoch": boundary,
        "snapshot_sha256": snapshot_sha,
        "idempotency_key": result.idempotency_key,
        "checkpoint": result.checkpoint,
        "bot_count": len(ledger_after),
        "files_verified_identical": len(after),
        "checkpoints": names_after,
    }


def cmd_replay_current(args) -> int:
    report = replay_current(args.state_root, args.experiment_id, args.config,
                            verify_code=args.verify_code)
    print(f"GEN2_CONTROL_REPLAY_OK experiment={report['experiment_id']} "
          f"status={report['status']} boundary={report['boundary_epoch']} "
          f"checkpoint={report['checkpoint']} "
          f"bots={report['bot_count']} "
          f"files_identical={report['files_verified_identical']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python tools/gen2_control.py",
        description="Generation-2 manual control-plane guard + replay-current + "
                    "pause (out-of-tree; never changes the source binding).")
    p.add_argument("--state-root", dest="state_root", default=_DEFAULT_STATE_ROOT,
                   help="root under which state/gen2/<id>/ lives (default: state)")
    p.add_argument("--config", default=_DEFAULT_CONFIG,
                   help=f"config path (default: {_DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="command", required=True)

    pg = sub.add_parser(
        "guard", help="read-only: assert an action is legal for an experiment")
    pg.add_argument("--action", required=True, choices=ACTIONS,
                    help="the control action whose legality to check")
    pg.add_argument("--experiment-id", dest="experiment_id", required=True,
                    help="the exact experiment id to check (no auto-discovery)")

    pr = sub.add_parser(
        "replay-current",
        help="read-only: prove CURRENT's boundary replays to ALREADY_PUBLISHED "
             "with zero on-disk change")
    pr.add_argument("--experiment-id", dest="experiment_id", required=True,
                    help="the exact ACTIVE experiment id whose CURRENT to replay")
    pr.add_argument("--no-verify-code", dest="verify_code", action="store_false",
                    default=True,
                    help="skip the source-binding/code-drift check (tests only; "
                         "production replays always verify the binding)")

    pp = sub.add_parser("pause", help="ACTIVE -> PAUSED (audited; refuses otherwise)")
    pp.add_argument("--experiment-id", dest="experiment_id", required=True,
                    help="the exact ACTIVE experiment id to pause")

    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    handlers = {"guard": cmd_guard, "pause": cmd_pause,
                "replay-current": cmd_replay_current}
    try:
        return handlers[args.command](args)
    except ControlRefused as e:
        sys.stderr.write(f"GEN2_CONTROL_REFUSED: {e.reason}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
