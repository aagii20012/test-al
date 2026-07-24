#!/usr/bin/env python3
"""Generation-2 manual control-plane helpers — OUTSIDE the bound source tree.

This module lives under ``tools/`` on purpose. The Generation-2 code binding is a
canonical hash of the ``algotrading/`` package ONLY (see
``algotrading/gen2/source_hash.py`` + ``source_inventory.json``). Anything added
here therefore CANNOT change ``source_tree_sha256`` and so can never invalidate a
PREPARED experiment's immutable binding — the exact experiment the operator is
about to activate stays activatable byte-for-byte.

It exposes the two things the GitHub manual-control workflow
(``.github/workflows/gen2-control.yml``) needs that the engine CLI does not
already provide:

  guard   READ-ONLY gatekeeper. Asserts that ``--experiment-id`` names a
          committed, non-retired, non-terminal experiment whose immutable binding
          still verifies, AND that the requested ``--action`` is legal for its
          current status. Mutates nothing; exits non-zero (fail closed) on any
          violation. The workflow runs this first, in a read-only job, before it
          is ever granted write permission.

  pause   ACTIVE -> PAUSED via the coordinator's own audited ``set_status``. This
          is the only lifecycle transition the engine CLI omits. It refuses a
          retired id or any status other than ACTIVE, so it can only ever halt a
          genuinely live experiment (never resurrect or mislabel one).

The consequential actions themselves (activate / canary-tick / verify-current)
are NOT reimplemented here: the workflow calls the existing, already-tested
``python -m algotrading.gen2`` subcommands for those, so activation still runs the
strict keyless Coinbase provenance gate + the source-binding check inside the
engine. This file only adds the guard and the missing pause transition.
"""

from __future__ import annotations

import argparse
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
from algotrading.gen2.coordinator import Gen2Coordinator  # noqa: E402
from algotrading.gen2.experiment import (  # noqa: E402
    RETIRED_EXPERIMENT_IDS, ExperimentManifest, Status)

_DEFAULT_STATE_ROOT = "state"
_DEFAULT_CONFIG = "config/config.ci.yaml"

# The tightly restricted action vocabulary the manual-control workflow may ask
# for, mapped to the ONLY experiment statuses under which each is legal.
#   activate : PREPARED -> ACTIVE          (launch switch; engine runs provenance)
#   canary   : one live tick               (requires an already-ACTIVE experiment)
#   pause    : ACTIVE   -> PAUSED          (halt a live experiment)
#   verify   : read-only integrity check   (any non-terminal, non-retired status)
ALLOWED_STATUS = {
    "activate": frozenset({Status.PREPARED}),
    "canary": frozenset({Status.ACTIVE}),
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python tools/gen2_control.py",
        description="Generation-2 manual control-plane guard + pause "
                    "(out-of-tree; never changes the source binding).")
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
    handlers = {"guard": cmd_guard, "pause": cmd_pause}
    try:
        return handlers[args.command](args)
    except ControlRefused as e:
        sys.stderr.write(f"GEN2_CONTROL_REFUSED: {e.reason}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
