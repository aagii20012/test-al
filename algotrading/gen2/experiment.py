"""Generation 2 experiment identity + immutable binding manifest.

An *experiment* binds together, at creation time and forever after:

  * the experiment id and the generation / schema markers;
  * the exact corrected code (git commit + a content hash of the ``algotrading``
    source tree) the results were produced by;
  * the config hash + the cost/risk model;
  * the market source, products, interval and warm-up depth;
  * the eight bot definitions (4 strategies x 2 coins), each funded $10,000;
  * the shared-decision-boundary rule and the idempotency rule;
  * the creation timestamp and the lifecycle status.

The id embeds a short hash of the code + config + bot set, so a different code
tree, config, or bot roster necessarily produces a different experiment id — you
cannot silently continue an experiment under changed code. The binding fields are
immutable: once PREPARED, only the ``status`` may change (recorded in the audit
log), and even a status change re-verifies that the bound code/config still
match (fail-closed on drift).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .. import state_schema
from ..research.grids import DEFAULT_PARAMS

# --------------------------------------------------------------------------
# Fixed experiment parameters (the approved Generation-2 design).
# --------------------------------------------------------------------------
STRATEGIES: List[str] = ["momentum", "rsi", "donchian", "bollinger"]
SYMBOLS: List[str] = ["BTCUSDT", "ETHUSDT"]
CAPITAL_PER_BOT: float = 10_000.0
INTERVAL: str = "1h"
HISTORY: int = 300           # warm-up depth per bot, mirrors cmd_tick --simulated

# Market data source: Coinbase public keyless candles (READ-ONLY market data).
MARKET_SOURCE = "coinbase-public"
PRODUCTS: Dict[str, str] = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD"}

# The production simulated cost model (identical to cmd_tick --simulated and to
# tools/run_falsification.py). Placed in the manifest so a reviewer sees exactly
# what the sim assumed.
COST_MODEL = {
    "commission_pct": 0.001,
    "slippage_bps": 2.0,
    "fill_at": "close",
    "min_notional": 10.0,
}

BOUNDARY_RULE = (
    "The shared decision hour is the latest closed 1h candle present in EVERY "
    "product. One coordinator tick advances all 8 bots on that single boundary "
    "or advances none of them."
)
IDEMPOTENCY_RULE = (
    "idempotency_key = experiment_id + ':' + shared_candle_epoch. A published "
    "key never re-advances any bot; per-bot last_bar_ts <= shared_candle_epoch "
    "is required to act, so a crash-resumed tick converges to the same result."
)


class Status:
    """Experiment lifecycle. Only these transitions are legal (see manifest)."""

    PREPARED = "PREPARED"   # built + bound, NOT trading (no scheduler acts on it)
    ACTIVE = "ACTIVE"       # explicitly launched (human-gated); live ticks allowed
    PAUSED = "PAUSED"       # temporarily halted; resumable to ACTIVE
    FAILED = "FAILED"       # a tick aborted; requires investigation before resume
    CLOSED = "CLOSED"       # terminal; no further ticks

    ALL = {PREPARED, ACTIVE, PAUSED, FAILED, CLOSED}


def _bot_id(strategy: str, symbol: str) -> str:
    return f"{strategy}_{symbol}"


# The canonical eight bot definitions, funded $10,000 each.
BOT_DEFS: List[dict] = [
    {
        "bot_id": _bot_id(strategy, symbol),
        "strategy": strategy,
        "symbol": symbol,
        "product": PRODUCTS[symbol],
        "params": dict(DEFAULT_PARAMS[strategy]),
        "initial_capital": CAPITAL_PER_BOT,
    }
    for strategy in STRATEGIES
    for symbol in SYMBOLS
]


# --------------------------------------------------------------------------
# Hashing helpers (all deterministic).
# --------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_canonical(obj) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def _package_dir() -> str:
    # .../algotrading/gen2/experiment.py -> .../algotrading
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source_tree_hash(package_dir: Optional[str] = None) -> Dict[str, object]:
    """Content hash of every ``*.py`` under the algotrading package.

    Independent of git: hashes sorted ``relpath:filehash`` pairs so a byte
    change anywhere in the corrected code changes the experiment id.
    """
    package_dir = package_dir or _package_dir()
    entries: List[str] = []
    for root, _dirs, files in os.walk(package_dir):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, package_dir).replace(os.sep, "/")
            with open(full, "rb") as fh:
                entries.append(f"{rel}:{sha256_bytes(fh.read())}")
    entries.sort()
    digest = sha256_bytes("\n".join(entries).encode("utf-8"))
    return {"sha256": digest, "file_count": len(entries)}


def git_commit(cwd: Optional[str] = None) -> Optional[str]:
    """Best-effort HEAD commit; ``None`` if not a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or _package_dir(),
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def strategy_source_hashes() -> Dict[str, str]:
    """Content hash of each strategy's source module (the bound strategy code)."""
    from ..research.grids import STRATEGY_REGISTRY

    hashes: Dict[str, str] = {}
    for key in STRATEGIES:
        cls = STRATEGY_REGISTRY[key]
        module = __import__(cls.__module__, fromlist=["__file__"])
        path = getattr(module, "__file__", None)
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                hashes[key] = sha256_bytes(fh.read())
        else:  # pragma: no cover - defensive
            hashes[key] = ""
    return hashes


def config_hash(config_path: str) -> Optional[str]:
    if config_path and os.path.exists(config_path):
        with open(config_path, "rb") as fh:
            return sha256_bytes(fh.read())
    return None


def _fmt_id_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_experiment_id(created: datetime, short_hash: str) -> str:
    """``gen2-YYYYMMDDTHHMMSSZ-<8 hex>``."""
    return f"{state_schema.GENERATION}-{_fmt_id_timestamp(created)}-{short_hash[:8]}"


# --------------------------------------------------------------------------
# The manifest.
# --------------------------------------------------------------------------
_IMMUTABLE_FIELDS = (
    "experiment_id", "generation", "schema_version", "created_utc",
    "code", "config", "cost_model", "market", "capital_per_bot",
    "bots", "strategies_sha256", "boundary_rule", "idempotency", "binding_sha256",
)


@dataclass
class ExperimentManifest:
    experiment_id: str
    generation: str
    schema_version: int
    status: str
    created_utc: str
    code: dict
    config: dict
    cost_model: dict
    market: dict
    capital_per_bot: float
    bots: List[dict]
    strategies_sha256: Dict[str, str]
    boundary_rule: str
    idempotency: str
    binding_sha256: str
    history: List[dict] = field(default_factory=list)  # status-transition log
    # NON-binding provenance: the commit that FLIPPED this experiment to ACTIVE.
    # Deliberately excluded from the immutable binding (a manifest cannot contain
    # the hash of the very commit that contains it), and unknown at prepare time.
    activation_commit: Optional[str] = None

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "generation": self.generation,
            "schema_version": self.schema_version,
            "status": self.status,
            "created_utc": self.created_utc,
            "code": self.code,
            "config": self.config,
            "cost_model": self.cost_model,
            "market": self.market,
            "capital_per_bot": self.capital_per_bot,
            "bots": self.bots,
            "strategies_sha256": self.strategies_sha256,
            "boundary_rule": self.boundary_rule,
            "idempotency": self.idempotency,
            "binding_sha256": self.binding_sha256,
            "history": self.history,
            "activation_commit": self.activation_commit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentManifest":
        return cls(
            experiment_id=d["experiment_id"],
            generation=d["generation"],
            schema_version=d["schema_version"],
            status=d["status"],
            created_utc=d["created_utc"],
            code=d["code"],
            config=d["config"],
            cost_model=d["cost_model"],
            market=d["market"],
            capital_per_bot=d["capital_per_bot"],
            bots=d["bots"],
            strategies_sha256=d["strategies_sha256"],
            boundary_rule=d["boundary_rule"],
            idempotency=d["idempotency"],
            binding_sha256=d["binding_sha256"],
            history=d.get("history", []),
            activation_commit=d.get("activation_commit"),
        )

    def binding_payload(self) -> dict:
        """The immutable fields that ``binding_sha256`` covers."""
        d = self.to_dict()
        return {k: d[k] for k in _IMMUTABLE_FIELDS if k != "binding_sha256"}

    def recompute_binding(self) -> str:
        return sha256_canonical(self.binding_payload())

    def verify_binding(self) -> None:
        """Fail closed if the immutable binding has been tampered with."""
        expect = self.recompute_binding()
        if expect != self.binding_sha256:
            raise state_schema.IncompatibleStateError(
                f"Manifest binding hash mismatch for {self.experiment_id!r}: "
                f"stored {self.binding_sha256!r} != recomputed {expect!r}. The "
                "immutable experiment definition has been altered.")

    def bot_ids(self) -> List[str]:
        return [b["bot_id"] for b in self.bots]


def build_manifest(
    *,
    created: datetime,
    config_path: str = "config/config.ci.yaml",
    config_extra: Optional[dict] = None,
    status: str = Status.PREPARED,
    implementation_commit: Optional[str] = None,
) -> ExperimentManifest:
    """Construct a fresh PREPARED manifest bound to the CURRENT code + config.

    ``implementation_commit`` is the git commit whose tree the bound
    ``source_tree_sha256`` was computed from. For a two-stage launch it is the
    *already-pushed* Stage-A commit, passed in explicitly by the operator so the
    binding refers to a commit that provably exists on the remote — NOT to the
    working-tree HEAD at build time. When omitted (ad-hoc / dormant prepare) it
    falls back to the local HEAD as a best-effort marker.

    The experiment id and binding depend only on ``source_tree_sha256`` (content),
    never on the commit hash, so binding to a commit that *contains this manifest*
    would be circular — it is not. ``activation_commit`` (the commit that later
    flips PREPARED->ACTIVE) is a separate, non-binding field set at activation.
    """
    tree = source_tree_hash()
    code = {
        "implementation_commit": (
            implementation_commit if implementation_commit is not None
            else git_commit()),
        "source_tree_sha256": tree["sha256"],
        "source_file_count": tree["file_count"],
    }
    config = {
        "path": config_path,
        "sha256": config_hash(config_path),
    }
    if config_extra:
        config.update(config_extra)

    strat_hashes = strategy_source_hashes()

    market = {
        "source": MARKET_SOURCE,
        "interval": INTERVAL,
        "history": HISTORY,
        "products": dict(PRODUCTS),
    }

    bots = []
    for b in BOT_DEFS:
        bot = dict(b)
        bot["params_sha256"] = sha256_canonical(bot["params"])
        bots.append(bot)

    # The short hash that makes the id code/config/bot-set specific.
    short_src = canonical_json({
        "code": code["source_tree_sha256"],
        "config": config["sha256"],
        "bots": [{"bot_id": b["bot_id"], "params_sha256": b["params_sha256"]}
                 for b in bots],
        "cost_model": COST_MODEL,
    })
    short_hash = sha256_bytes(short_src.encode("utf-8"))
    experiment_id = make_experiment_id(created, short_hash)

    manifest = ExperimentManifest(
        experiment_id=experiment_id,
        generation=state_schema.GENERATION,
        schema_version=state_schema.SCHEMA_VERSION,
        status=status,
        created_utc=created.astimezone(timezone.utc).isoformat(),
        code=code,
        config=config,
        cost_model=dict(COST_MODEL),
        market=market,
        capital_per_bot=CAPITAL_PER_BOT,
        bots=bots,
        strategies_sha256=strat_hashes,
        boundary_rule=BOUNDARY_RULE,
        idempotency=IDEMPOTENCY_RULE,
        binding_sha256="",
    )
    manifest.binding_sha256 = manifest.recompute_binding()
    return manifest
