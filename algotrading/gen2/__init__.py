"""Generation 2 experiment machinery (PREPARE + local build/test only).

This package is deliberately isolated from Generation 1. It NEVER reads or
writes any Generation-1 state file (``state/*_sim.json``); everything lives
under ``state/gen2/<experiment_id>/``. It imports only market-data *readers*
and the *simulated* execution handler — never a live/order-placing exchange or
execution path (see ``tests/test_gen2_no_order_endpoints.py``).

Nothing in this package activates trading. An experiment is created in the
PREPARED status; a coordinator tick refuses to run "live" until the experiment
is explicitly ACTIVE, and activation is a separate, human-gated step.
"""

from .experiment import (
    BOT_DEFS,
    ExperimentManifest,
    Status,
    build_manifest,
    make_experiment_id,
)

__all__ = [
    "BOT_DEFS",
    "ExperimentManifest",
    "Status",
    "build_manifest",
    "make_experiment_id",
]
