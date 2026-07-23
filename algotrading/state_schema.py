"""Fail-closed generation / schema boundary for tick state files.

Every persisted tick checkpoint carries an explicit experiment *generation* and a
*schema version*. The corrected runner refuses to load anything that is not an
exact match: Generation 1 files (which carry no marker at all) and any older or
newer schema are rejected loudly instead of being silently resumed, migrated, or
repaired.

Why fail closed: Generation 1 is invalidated forensic evidence produced by code
with a position-desync bug and a reversal cost-basis bug. Resuming a Gen1 blob in
the corrected code would (a) silently continue an invalid experiment on a wrong
cost basis and (b) risk overwriting the evidence. There is deliberately **no**
migration path — a new experiment starts from a freshly initialised state.
"""

from __future__ import annotations

# Bump GENERATION whenever results must not be mixed with a prior run; bump
# SCHEMA_VERSION whenever the on-disk state layout changes incompatibly.
GENERATION = "gen2"
SCHEMA_VERSION = 2

# Generation 1 carried no marker at all. Named only for clear diagnostics.
LEGACY_GENERATION = "gen1"


class IncompatibleStateError(RuntimeError):
    """A state file is not an exact Generation-2 / current-schema match."""


def stamp(state: dict) -> dict:
    """Set the current generation + schema markers on `state` (in place)."""
    state["generation"] = GENERATION
    state["schema_version"] = SCHEMA_VERSION
    return state


def is_current(state) -> bool:
    """True only for an exact Generation-2 / current-schema blob."""
    return (
        isinstance(state, dict)
        and state.get("generation") == GENERATION
        and state.get("schema_version") == SCHEMA_VERSION
    )


def validate_loadable(state: dict, path: str) -> None:
    """Fail closed unless `state` is an exact current-generation/schema blob.

    No migration, no repair, no inference — an incompatible file is rejected with
    a clear incompatibility error.
    """
    if is_current(state):
        return
    gen = state.get("generation") if isinstance(state, dict) else None
    ver = state.get("schema_version") if isinstance(state, dict) else None
    if gen is None and ver is None:
        detail = ("it carries no generation marker at all, which identifies it "
                  "as a Generation 1 (invalidated) file")
    else:
        detail = f"it is generation={gen!r} schema_version={ver!r}"
    raise IncompatibleStateError(
        f"Refusing to load state file {path!r}: {detail}. The corrected runner "
        f"requires generation={GENERATION!r} schema_version={SCHEMA_VERSION}. "
        "Generation 1 files are invalidated forensic evidence and are never "
        "resumed, migrated, repaired, or overwritten. To begin Generation 2, "
        "initialise a fresh state explicitly (see the launch gate).")


def ensure_fresh_start_allowed(path: str, allowed: bool) -> None:
    """Gate the first-ever run: a fresh Gen2 state needs explicit approval.

    Prevents a scheduler (cron / CI) from silently *launching* a new experiment
    the moment a state file is absent. `allowed` is set only by an explicit
    operator flag / env var once Generation 2 launch has been approved.
    """
    if allowed:
        return
    raise IncompatibleStateError(
        f"No state file at {path!r}. Refusing to initialise a fresh Generation 2 "
        "experiment without explicit launch approval. Pass "
        "--allow-fresh-generation (or set ALGOTRADING_ALLOW_FRESH_GEN2=1) once "
        "Generation 2 launch has been approved.")
