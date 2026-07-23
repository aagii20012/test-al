"""The Generation-2 single coordinator.

ONE process runs a whole tick for all 8 bots. It never spawns 8 processes and
never advances a subset:

  1. READ CURRENT — resolve the single published-state pointer and load every
     bot's prior state from EXACTLY that immutable checkpoint (manifest hash +
     every artifact hash verified). Never scan for a "latest" file.
  2. FETCH ONCE   — pull closed candles for every product a single time.
  3. SNAPSHOT     — validate, pick the one shared decision hour, freeze + hash an
                    immutable market snapshot all 8 bots read.
  4. COMPUTE ALL  — run every bot in memory against that snapshot, reusing the
                    exact tested decision path of ``cmd_tick --simulated``
                    (LiveDataHandler warm-up -> one injected MarketEvent ->
                    dispatch_pending). If ANY required bot raises or fails
                    validation, the tick ABORTS and NOTHING is published.
  5. PUBLISH ATOMICALLY — compute every artifact into a NEW temporary checkpoint
                    directory, hash each one into CHECKPOINT_MANIFEST.json, fsync,
                    atomically rename the temp dir to its immutable content-
                    addressed name, re-verify it, and finally replace the single
                    CURRENT pointer LAST. A crash at any step leaves CURRENT
                    resolving to the complete OLD checkpoint or the complete NEW
                    one — never a mix. See ``checkpoint.py`` for the on-disk format.

Isolation & safety:
  * Reads/writes ONLY under ``<state_root>/gen2/<experiment_id>/``. Never touches
    a Generation-1 file.
  * Imports only market-data readers + the SIMULATED execution handler. No live
    exchange, no order endpoint, no credentials (see the no-order-endpoints test).
  * Fail-closed generation / schema / experiment-id / bot-id / code-drift /
    checkpoint-integrity checks on every load; orphans are never silently deleted.
  * Refuses a live tick unless the experiment is explicitly ACTIVE; a PREPARED
    experiment only permits dry-runs.
"""

from __future__ import annotations

import errno
import json
import math
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .. import state_schema
from ..core.event_queue import EventQueue
from ..core.events import MarketEvent
from ..engine.loop import dispatch_pending
from ..execution.simulated import SimulatedExecutionHandler
from ..portfolio.portfolio import Portfolio
from ..research.grids import STRATEGY_REGISTRY
from ..risk.risk_manager import RiskConfig, RiskManager
from ..utils.config import load_config
from ..data.live import LiveDataHandler
from . import checkpoint as cp
from . import experiment as exp
from .checkpoint import CheckpointError
from .experiment import ExperimentManifest, Status
from .snapshot import MarketSnapshot, SnapshotExchange, build_snapshot

_PERIODS_PER_YEAR_1H = 365 * 24
_LOCK_TTL_SECONDS = 900          # a lock older than this is treated as stale
_RECON_TOL = 1e-4                # $ tolerance for the accounting identity


class Gen2Error(RuntimeError):
    pass


class NotActivatedError(Gen2Error):
    """A live tick was requested on an experiment that is not ACTIVE."""


class OverlappingRunError(Gen2Error):
    """Another coordinator run holds the experiment lock."""


class StaleRunError(Gen2Error):
    """A run tried to publish a checkpoint no newer than the one already CURRENT.

    This is the last-line defence against a slow/old coordinator overwriting the
    result of a newer one: the CURRENT pointer only ever moves forward, and never
    by clobbering a lineage this run did not compute against.
    """


class TickAborted(Gen2Error):
    """A required bot failed; the whole tick was aborted with zero publication."""

    def __init__(self, message: str, bot_id: Optional[str] = None):
        super().__init__(message)
        self.bot_id = bot_id


@dataclass
class BotResult:
    bot_id: str
    strategy: str
    symbol: str
    acted: bool                 # always True in the checkpoint model (whole
                                # checkpoint advances atomically; kept for the
                                # scoreboard / audit summary shape).
    last_bar_ts: int
    equity: float
    cash: float
    position: float
    realized_pnl: float
    total_commission: float
    last_price: float
    recon_residual: float
    state: dict = field(repr=False, default_factory=dict)

    def summary(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "acted": self.acted,
            "last_bar_ts": self.last_bar_ts,
            "equity": self.equity,
            "cash": self.cash,
            "position": self.position,
            "realized_pnl": self.realized_pnl,
            "total_commission": self.total_commission,
            "last_price": self.last_price,
            "recon_residual": self.recon_residual,
        }


@dataclass
class TickResult:
    status: str                 # PUBLISHED | ALREADY_PUBLISHED
    experiment_id: str
    decision_epoch_ms: int
    idempotency_key: str
    snapshot_sha256: str
    dry_run: bool
    bots: List[dict]
    checkpoint: Optional[str] = None
    prior_checkpoint: Optional[str] = None
    checkpoint_manifest_sha256: Optional[str] = None


class Gen2Coordinator:
    def __init__(
        self,
        manifest: ExperimentManifest,
        state_root: str = "state",
        *,
        config_path: str = "config/config.ci.yaml",
        fetch_ohlcv: Optional[Callable[..., object]] = None,
        allow_fresh: bool = False,
        verify_code: bool = True,
    ):
        manifest.verify_binding()
        self.manifest = manifest
        self.state_root = state_root
        self.config_path = config_path
        self._fetch_ohlcv = fetch_ohlcv
        self.allow_fresh = allow_fresh
        self.verify_code = verify_code
        # TEST-ONLY crash injection: a callable taking a stage name. It may raise
        # to simulate a process death at that exact point in the publish protocol.
        self._crash_hook: Optional[Callable[[str], None]] = None

    # ---- paths -------------------------------------------------------------
    @property
    def exp_dir(self) -> str:
        return os.path.join(self.state_root, state_schema.GENERATION,
                            self.manifest.experiment_id)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.exp_dir, "manifest.json")

    @property
    def checkpoints_dir(self) -> str:
        return cp.checkpoints_dir(self.exp_dir)

    @property
    def current_path(self) -> str:
        return cp.current_path(self.exp_dir)

    @property
    def lock_path(self) -> str:
        return os.path.join(self.exp_dir, ".lock")

    def checkpoint_dir(self, name: str) -> str:
        return cp.checkpoint_dir(self.exp_dir, name)

    # ---- filesystem helpers ------------------------------------------------
    def _ensure_dirs(self) -> None:
        os.makedirs(self.exp_dir, exist_ok=True)
        os.makedirs(self.checkpoints_dir, exist_ok=True)

    @staticmethod
    def _atomic_write(path: str, obj: dict) -> str:
        """Write JSON atomically (tmp + os.replace). Returns sha256 of the bytes."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        text = json.dumps(obj, indent=2, sort_keys=True, default=str)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return exp.sha256_bytes(text.encode("utf-8"))

    @staticmethod
    def _write_artifact(path: str, content) -> None:
        """Write one checkpoint artifact (dict -> canonical-ish JSON; str/bytes
        verbatim) and fsync it. Verbatim writes let tests seed intentionally
        malformed (but hash-consistent) artifacts."""
        if isinstance(content, (bytes, bytearray)):
            raw = bytes(content)
        elif isinstance(content, str):
            raw = content.encode("utf-8")
        else:
            raw = json.dumps(content, indent=2, sort_keys=True,
                             default=str).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _fsync_dir(path: str) -> None:
        """Best-effort directory fsync (unsupported on Windows dir handles)."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _crash(self, stage: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(stage)

    # ---- lifecycle ---------------------------------------------------------
    def prepare(self) -> str:
        """Write the PREPARED manifest + scaffolding. Refuses to clobber."""
        if os.path.exists(self.manifest_path):
            raise Gen2Error(
                f"Experiment {self.manifest.experiment_id!r} already prepared at "
                f"{self.manifest_path!r}; refusing to overwrite.")
        self._ensure_dirs()
        self._atomic_write(self.manifest_path, self.manifest.to_dict())
        return self.manifest_path

    def load_manifest_from_disk(self) -> ExperimentManifest:
        with open(self.manifest_path, "r", encoding="utf-8") as fh:
            m = ExperimentManifest.from_dict(json.load(fh))
        m.verify_binding()
        if m.experiment_id != self.manifest.experiment_id:
            raise state_schema.IncompatibleStateError(
                f"On-disk manifest id {m.experiment_id!r} != coordinator id "
                f"{self.manifest.experiment_id!r}.")
        return m

    def _verify_code_binding(self) -> None:
        if not self.verify_code:
            return
        tree = exp.source_tree_hash()
        bound = self.manifest.code.get("source_tree_sha256")
        if tree["sha256"] != bound:
            raise state_schema.IncompatibleStateError(
                "Code drift: the running algotrading source tree hashes to "
                f"{tree['sha256']!r} but the experiment is bound to {bound!r}. "
                "Refusing to continue an experiment under changed code.")

    def set_status(self, new_status: str, *, approved: bool = False) -> None:
        """Persist a lifecycle transition (audited). ACTIVE requires approval."""
        if new_status not in Status.ALL:
            raise Gen2Error(f"Unknown status {new_status!r}")
        m = self.load_manifest_from_disk()
        if new_status == Status.ACTIVE:
            if not approved:
                raise NotActivatedError(
                    "Activating Generation 2 is a human-gated launch step; refusing "
                    "to set ACTIVE without explicit approval=True.")
            self._verify_code_binding()
            # Record (non-binding) the commit that flipped this experiment live.
            m.activation_commit = exp.git_commit()
        transition = {"from": m.status, "to": new_status,
                      "approved": bool(approved)}
        m.status = new_status
        m.history = list(m.history) + [transition]
        self.manifest = m
        self._atomic_write(self.manifest_path, m.to_dict())

    # ---- lock --------------------------------------------------------------
    @contextmanager
    def _run_lock(self, now: datetime):
        os.makedirs(self.exp_dir, exist_ok=True)
        path = self.lock_path
        acquired = False
        try:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # Lock exists — steal only if clearly stale.
                if self._lock_is_stale(path, now):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                else:
                    raise OverlappingRunError(
                        f"Another coordinator run holds {path!r}; refusing to run a "
                        "concurrent tick (overlapping-workflow protection).")
            with os.fdopen(fd, "w") as fh:
                json.dump({"pid": os.getpid(),
                           "acquired_utc": now.astimezone(timezone.utc).isoformat()}, fh)
            acquired = True
            yield
        finally:
            if acquired:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _lock_is_stale(self, path: str, now: datetime) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            acquired = datetime.fromisoformat(info["acquired_utc"])
            age = (now - acquired).total_seconds()
            return age > _LOCK_TTL_SECONDS
        except Exception:
            return True   # unreadable lock -> stale

    # ---- risk config -------------------------------------------------------
    def _risk_config(self) -> RiskConfig:
        cfg = load_config(self.config_path)
        r = cfg.risk or {}
        return RiskConfig(
            max_position_pct=r.get("max_position_pct", 0.20),
            risk_per_trade=r.get("risk_per_trade", 0.10),
            max_leverage=r.get("max_leverage", 1.0),
            allow_short=r.get("allow_short", True),
            cash_buffer=r.get("cash_buffer", 0.0),
            use_stops=r.get("use_stops", True),
            stop_loss_pct=r.get("stop_loss_pct", 0.05),
            take_profit_pct=r.get("take_profit_pct", 0.0),
            trailing_stop_pct=r.get("trailing_stop_pct", 0.0),
            atr_sizing=r.get("atr_sizing", False),
            atr_period=r.get("atr_period", 14),
            atr_stop_mult=r.get("atr_stop_mult", 2.0),
            max_daily_loss_pct=r.get("max_daily_loss_pct", 0.0),
            max_daily_profit_pct=r.get("max_daily_profit_pct", 0.0),
            max_drawdown_pct=r.get("max_drawdown_pct", 0.0),
        )

    def _financing_apr(self) -> float:
        return load_config(self.config_path).financing_apr

    # ---- per-bot state -----------------------------------------------------
    def _stamp_bot_state(self, state: dict, bot_id: str, decision_epoch_ms: int,
                         now: datetime, dry_run: bool) -> dict:
        state_schema.stamp(state)
        state["experiment_id"] = self.manifest.experiment_id
        state["bot_id"] = bot_id
        state["last_bar_ts"] = decision_epoch_ms
        state["updated_utc"] = now.astimezone(timezone.utc).isoformat()
        state["dry_run"] = bool(dry_run)
        return state

    def _coerce_prev_state(self, state, bot_id: str) -> dict:
        """Fail-closed schema/identity check on a prior bot state.

        The state has already survived checkpoint-integrity verification (its
        bytes hash to the manifest); this second layer distinguishes a
        semantically incompatible prior state (wrong generation / schema /
        experiment / bot) from a merely corrupted artifact. No migration.
        """
        where = f"<checkpoint bot {bot_id}>"
        if not isinstance(state, dict):
            raise state_schema.IncompatibleStateError(
                f"Prior bot state {where} is not a JSON object.")
        state_schema.validate_loadable(state, where)      # generation + schema
        if state.get("experiment_id") != self.manifest.experiment_id:
            raise state_schema.IncompatibleStateError(
                f"Prior bot state {where} belongs to experiment "
                f"{state.get('experiment_id')!r}, not {self.manifest.experiment_id!r}.")
        if state.get("bot_id") != bot_id:
            raise state_schema.IncompatibleStateError(
                f"Prior bot state {where} carries bot_id {state.get('bot_id')!r}, "
                f"expected {bot_id!r}.")
        return state

    # ---- run one bot against the frozen snapshot ---------------------------
    def _run_bot(self, bot_def: dict, snapshot: MarketSnapshot,
                 decision_epoch_ms: int, prev_state: Optional[dict]) -> BotResult:
        """Mirror cmd_tick --simulated for exactly one bot, one decision bar.

        Reuses LiveDataHandler (warm-up from the frozen snapshot frame) + a single
        injected MarketEvent + dispatch_pending: the identical, already-tested
        decision path. Raises on any failure (caller aborts the whole tick).
        """
        bot_id = bot_def["bot_id"]
        symbol = bot_def["symbol"]
        strategy_key = bot_def["strategy"]
        params = bot_def["params"]
        capital = float(bot_def["initial_capital"])

        events = EventQueue()
        exchange = SnapshotExchange(snapshot)
        data = LiveDataHandler(events, exchange, [symbol],
                               interval=snapshot.interval,
                               history=self.manifest.market["history"],
                               drop_forming=False)
        risk = RiskManager(self._risk_config())
        portfolio = Portfolio(data, events, risk, initial_capital=capital,
                              financing_apr=self._financing_apr(),
                              periods_per_year=_PERIODS_PER_YEAR_1H)
        strategy = STRATEGY_REGISTRY[strategy_key](data, events, **params)

        if prev_state is not None:
            portfolio.load_state(prev_state["portfolio"])
            risk.load_state(prev_state["risk"])
            strategy.load_state(prev_state.get("strategy", {}))

        latest = data.get_latest_bar(symbol)
        if latest is None:
            raise TickAborted(f"{bot_id}: no market data for {symbol}", bot_id)
        latest_ts = int(latest.dt.timestamp() * 1000)
        if latest_ts != decision_epoch_ms:
            raise TickAborted(
                f"{bot_id}: snapshot latest bar {latest_ts} != shared decision "
                f"boundary {decision_epoch_ms}", bot_id)

        # Decision. Portfolio-authoritative sync happens inside dispatch_pending.
        events.put(MarketEvent(dt=latest.dt))
        dispatch_pending(events, strategy, portfolio, execution=SimulatedExecutionHandler(
            events, data, commission_pct=self.manifest.cost_model["commission_pct"],
            slippage_bps=self.manifest.cost_model["slippage_bps"],
            fill_at=self.manifest.cost_model["fill_at"],
            min_notional=self.manifest.cost_model["min_notional"]))

        state = {
            "portfolio": portfolio.dump_state(),
            "risk": risk.dump_state(),
            "strategy": strategy.dump_state(),
            "equity_now": portfolio.equity,
            "cash_now": portfolio.cash,
            "last_price": latest.close,
        }
        residual = self._reconciliation_residual(portfolio, data)
        return BotResult(
            bot_id=bot_id, strategy=strategy_key, symbol=symbol, acted=True,
            last_bar_ts=decision_epoch_ms, equity=portfolio.equity,
            cash=portfolio.cash, position=portfolio.position(symbol),
            realized_pnl=portfolio.realized_pnl,
            total_commission=portfolio.total_commission,
            last_price=latest.close, recon_residual=residual, state=state)

    @staticmethod
    def _reconciliation_residual(portfolio: Portfolio, data) -> float:
        """|equity - (IC + realized - commission - financing + unrealized)|."""
        unrealized = 0.0
        for sym, qty in portfolio.positions.items():
            if qty == 0:
                continue
            bar = data.get_latest_bar(sym)
            price = bar.close if bar else portfolio.avg_price.get(sym, 0.0)
            unrealized += (price - portfolio.avg_price.get(sym, 0.0)) * qty
        expected = (portfolio.initial_capital + portfolio.realized_pnl
                    - portfolio.total_commission - portfolio.total_financing
                    + unrealized)
        return abs(portfolio.equity - expected)

    def _validate_result(self, r: BotResult) -> None:
        for name, val in (("equity", r.equity), ("cash", r.cash),
                          ("position", r.position), ("last_price", r.last_price)):
            if not math.isfinite(val):
                raise TickAborted(f"{r.bot_id}: non-finite {name}={val}", r.bot_id)
        if r.recon_residual > _RECON_TOL:
            raise TickAborted(
                f"{r.bot_id}: accounting reconciliation off by "
                f"{r.recon_residual:.6f} (> {_RECON_TOL})", r.bot_id)

    # ---- reading published state (the ONLY way to read; verifies hashes) ---
    def read_current(self) -> Optional[cp.CurrentRef]:
        return cp.read_current(self.exp_dir, experiment_id=self.manifest.experiment_id)

    def resolve_current(self) -> Optional[cp.Checkpoint]:
        return cp.resolve_current(self.exp_dir, experiment_id=self.manifest.experiment_id)

    # ---- the tick ----------------------------------------------------------
    def run_tick(self, *, dry_run: bool = False,
                 now: Optional[datetime] = None) -> TickResult:
        now = now or datetime.now(timezone.utc)

        # 0. Preconditions (raise loudly — never silently no-op a live tick).
        manifest = self.load_manifest_from_disk()
        self.manifest = manifest
        if dry_run:
            if manifest.status == Status.CLOSED:
                raise NotActivatedError("Experiment is CLOSED; no ticks allowed.")
        else:
            if manifest.status != Status.ACTIVE:
                raise NotActivatedError(
                    f"Live tick requires status ACTIVE; experiment is "
                    f"{manifest.status!r}. Activate it (human-gated) first, or run "
                    "with dry_run=True.")
        self._verify_code_binding()

        with self._run_lock(now):
            return self._run_tick_locked(dry_run=dry_run, now=now)

    def _run_tick_locked(self, *, dry_run: bool, now: datetime) -> TickResult:
        exp_id = self.manifest.experiment_id
        self._ensure_dirs()

        # 1. Resolve the single published pointer; fail closed if it is corrupt.
        current = self.read_current()

        # Load prior bot state ONLY from the checkpoint CURRENT names (with full
        # manifest + artifact hash verification). Never scan for a latest file.
        prev_states: Dict[str, dict] = {}
        prior_name: Optional[str] = None
        prev_checkpoint: Optional[cp.Checkpoint] = None
        if current is not None:
            prior_name = current.checkpoint
            prev_checkpoint = cp.load_checkpoint(
                self.exp_dir, current.checkpoint, experiment_id=exp_id,
                expected_manifest_sha=current.checkpoint_manifest_sha256)
            prev_states = prev_checkpoint.bot_states

        # 2+3. Fetch once + freeze the shared, hashed snapshot.
        snapshot = build_snapshot(
            exp_id, self.manifest.market["products"],
            fetch_ohlcv=self._fetch(), interval=self.manifest.market["interval"],
            history=self.manifest.market["history"])
        epoch = snapshot.shared_candle_epoch_ms
        idem_key = f"{exp_id}:{epoch}"

        # Idempotency + monotonicity against the published pointer.
        if current is not None:
            if epoch == current.boundary_epoch:
                if snapshot.sha256 != current.snapshot_sha256:
                    raise state_schema.IncompatibleStateError(
                        f"Boundary {epoch} is already published but the fetched "
                        f"snapshot hash {snapshot.sha256!r} differs from the "
                        f"published {current.snapshot_sha256!r}; the market data "
                        "changed under a frozen boundary.")
                # This exact boundary is already published — idempotent no-op.
                return TickResult(
                    status="ALREADY_PUBLISHED", experiment_id=exp_id,
                    decision_epoch_ms=epoch, idempotency_key=idem_key,
                    snapshot_sha256=snapshot.sha256, dry_run=current.dry_run,
                    bots=prev_checkpoint.run_status.get("bots", []),
                    checkpoint=current.checkpoint, prior_checkpoint=current.prior_checkpoint,
                    checkpoint_manifest_sha256=current.checkpoint_manifest_sha256)
            if epoch < current.boundary_epoch:
                raise StaleRunError(
                    f"Fetched boundary {epoch} is older than the published boundary "
                    f"{current.boundary_epoch}; refusing to regress CURRENT.")
        else:
            # No published checkpoint yet — this is a fresh first tick.
            state_schema.ensure_fresh_start_allowed(self.current_path, self.allow_fresh)

        # 3. COMPUTE ALL bots in memory. Any failure -> abort, ZERO publication.
        results: List[BotResult] = []
        for bot_def in self.manifest.bots:
            bot_id = bot_def["bot_id"]
            if current is None:
                prev = None
            else:
                prev = prev_states.get(bot_id)
                if prev is None:
                    raise state_schema.IncompatibleStateError(
                        f"Published checkpoint {prior_name!r} has no state for bot "
                        f"{bot_id!r}; roster/checkpoint mismatch — refusing to run.")
                self._coerce_prev_state(prev, bot_id)
            r = self._run_bot(bot_def, snapshot, epoch, prev)
            self._validate_result(r)
            results.append(r)

        # 4. PUBLISH: build an immutable checkpoint, then flip CURRENT last.
        final_name = cp.checkpoint_name(epoch, snapshot.sha256)
        # An orphan may be reused ONLY if it matches this publication exactly.
        expected = {"prior_checkpoint": prior_name, "dry_run": bool(dry_run),
                    "code": self.manifest.code}
        if os.path.isdir(self.checkpoint_dir(final_name)):
            cp_manifest_sha = cp.validate_checkpoint(
                self.exp_dir, final_name, experiment_id=exp_id, expected=expected)
        else:
            artifacts = self._build_artifacts(
                results, snapshot, epoch, idem_key, prior_name, dry_run, now)
            self._write_checkpoint_dir(
                final_name, artifacts, epoch, snapshot.sha256, prior_name,
                dry_run, now, idem_key)
            # Re-verify the just-materialised checkpoint before pointing at it.
            cp_manifest_sha = cp.validate_checkpoint(
                self.exp_dir, final_name, experiment_id=exp_id, expected=expected)

        ref = cp.CurrentRef(
            experiment_id=exp_id, checkpoint=final_name,
            checkpoint_manifest_sha256=cp_manifest_sha, boundary_epoch=epoch,
            snapshot_sha256=snapshot.sha256, prior_checkpoint=prior_name,
            dry_run=bool(dry_run),
            published_utc=now.astimezone(timezone.utc).isoformat())
        self._swing_current(ref, prior_name)

        return TickResult(
            status="PUBLISHED", experiment_id=exp_id, decision_epoch_ms=epoch,
            idempotency_key=idem_key, snapshot_sha256=snapshot.sha256,
            dry_run=bool(dry_run), bots=[r.summary() for r in results],
            checkpoint=final_name, prior_checkpoint=prior_name,
            checkpoint_manifest_sha256=cp_manifest_sha)

    # ---- checkpoint publication --------------------------------------------
    def _build_artifacts(self, results: List[BotResult], snapshot: MarketSnapshot,
                         epoch: int, idem_key: str, prior_name: Optional[str],
                         dry_run: bool, now: datetime) -> Dict[str, object]:
        """Assemble the full artifact map for one checkpoint (relpath -> content)."""
        summaries = [r.summary() for r in results]
        artifacts: Dict[str, object] = {cp.SNAPSHOT_NAME: snapshot.to_dict()}
        for r in results:
            self._stamp_bot_state(r.state, r.bot_id, r.last_bar_ts, now, dry_run)
            artifacts[f"{cp.BOTS_DIRNAME}/{r.bot_id}.json"] = r.state
        artifacts[f"{cp.AUDIT_DIRNAME}/tick.json"] = {
            "experiment_id": self.manifest.experiment_id,
            "generation": state_schema.GENERATION,
            "schema_version": state_schema.SCHEMA_VERSION,
            "decision_epoch_ms": epoch,
            "idempotency_key": idem_key,
            "snapshot_sha256": snapshot.sha256,
            "snapshot_missing": snapshot.missing,
            "prior_checkpoint": prior_name,
            "code": self.manifest.code,
            "cost_model": self.manifest.cost_model,
            "published_utc": now.astimezone(timezone.utc).isoformat(),
            "dry_run": bool(dry_run),
            "bots": summaries,
        }
        artifacts[cp.RUN_STATUS_NAME] = {
            "status": "PUBLISHED",
            "experiment_id": self.manifest.experiment_id,
            "decision_epoch_ms": epoch,
            "idempotency_key": idem_key,
            "snapshot_sha256": snapshot.sha256,
            "prior_checkpoint": prior_name,
            "published_utc": now.astimezone(timezone.utc).isoformat(),
            "dry_run": bool(dry_run),
            "bots": summaries,
        }
        return artifacts

    def _write_checkpoint_dir(self, final_name: str, artifacts: Dict[str, object],
                              epoch: int, snapshot_sha256: str,
                              prior_name: Optional[str], dry_run: bool,
                              now: datetime, idem_key: str) -> None:
        """Stage every artifact in a fresh temp dir, hash + fsync, atomically
        rename to the immutable final name. A crash before the rename leaves only
        a ``.staging-*`` dir (invisible to readers); a crash after leaves a
        complete orphan (also invisible until CURRENT points at it)."""
        staging = cp.staging_dir(self.exp_dir, final_name)
        if os.path.exists(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)

        for rel in sorted(artifacts):
            full = os.path.join(staging, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            self._write_artifact(full, artifacts[rel])
        self._crash("after_stage_artifacts")

        artifact_hashes = cp.hash_dir(staging)
        checkpoint_manifest = {
            "experiment_id": self.manifest.experiment_id,
            "generation": state_schema.GENERATION,
            "schema_version": state_schema.SCHEMA_VERSION,
            "checkpoint": final_name,
            "boundary_epoch": int(epoch),
            "snapshot_sha256": snapshot_sha256,
            "prior_checkpoint": prior_name,
            "dry_run": bool(dry_run),
            "idempotency_key": idem_key,
            "code": self.manifest.code,
            "cost_model": self.manifest.cost_model,
            "created_utc": now.astimezone(timezone.utc).isoformat(),
            "artifacts": artifact_hashes,
        }
        self._write_artifact(
            os.path.join(staging, cp.CHECKPOINT_MANIFEST_NAME), checkpoint_manifest)
        self._crash("after_checkpoint_manifest")

        self._fsync_dir(staging)
        self._crash("after_fsync")

        self._crash("before_rename")
        os.rename(staging, self.checkpoint_dir(final_name))
        self._fsync_dir(self.checkpoints_dir)
        self._crash("after_rename")

    def _swing_current(self, ref: cp.CurrentRef, expected_prior: Optional[str]) -> None:
        """Replace the single CURRENT pointer LAST, atomically and monotonically.

        Re-reads the live pointer immediately before the flip: it never regresses
        the boundary and never clobbers a lineage this run did not compute against
        (a slower/older coordinator therefore fails closed instead of overwriting a
        newer publication). No force, no partial write — ``os.replace`` is atomic.
        """
        self._crash("before_current")
        live = self.read_current()
        if live is not None:
            if live.checkpoint == ref.checkpoint:
                return   # someone already published exactly this checkpoint
            if live.boundary_epoch >= ref.boundary_epoch:
                raise StaleRunError(
                    f"CURRENT is already at boundary {live.boundary_epoch} "
                    f">= this run's {ref.boundary_epoch}; refusing to publish an "
                    "older checkpoint over a newer one (no clobber, no force).")
            if live.checkpoint != expected_prior:
                raise StaleRunError(
                    f"CURRENT advanced to {live.checkpoint!r} while this run computed "
                    f"against {expected_prior!r}; recompute against the new base "
                    "rather than clobbering it.")

        path = self.current_path
        tmp = path + ".tmp"
        text = json.dumps(ref.to_dict(), indent=2, sort_keys=True, default=str)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        self._crash("during_current")
        os.replace(tmp, path)
        self._fsync_dir(self.exp_dir)
        self._crash("after_current")

    # ---- TEST SUPPORT ------------------------------------------------------
    def seed_checkpoint(self, *, bot_payloads: Dict[str, object],
                        snapshot: MarketSnapshot, dry_run: bool = True,
                        prior: Optional[str] = None,
                        now: Optional[datetime] = None) -> str:
        """TEST-ONLY: publish a hash-consistent checkpoint from arbitrary bot
        payloads (which may be intentionally corrupt / gen1 / unmarked), then flip
        CURRENT to it. Lets a test drive the per-bot schema layer independently of
        the checkpoint-integrity layer. Returns the checkpoint name."""
        now = now or datetime.now(timezone.utc)
        self._ensure_dirs()
        epoch = snapshot.shared_candle_epoch_ms
        idem_key = f"{self.manifest.experiment_id}:{epoch}"
        summaries: List[dict] = []
        artifacts: Dict[str, object] = {cp.SNAPSHOT_NAME: snapshot.to_dict()}
        for bot_id, payload in bot_payloads.items():
            artifacts[f"{cp.BOTS_DIRNAME}/{bot_id}.json"] = payload
        artifacts[f"{cp.AUDIT_DIRNAME}/tick.json"] = {
            "experiment_id": self.manifest.experiment_id,
            "decision_epoch_ms": epoch, "seeded": True, "bots": summaries}
        artifacts[cp.RUN_STATUS_NAME] = {
            "status": "PUBLISHED", "experiment_id": self.manifest.experiment_id,
            "decision_epoch_ms": epoch, "snapshot_sha256": snapshot.sha256,
            "prior_checkpoint": prior, "dry_run": bool(dry_run),
            "published_utc": now.astimezone(timezone.utc).isoformat(),
            "bots": summaries}
        final_name = cp.checkpoint_name(epoch, snapshot.sha256)
        self._write_checkpoint_dir(
            final_name, artifacts, epoch, snapshot.sha256, prior, dry_run,
            now, idem_key)
        cp_manifest_sha = cp.validate_checkpoint(
            self.exp_dir, final_name, experiment_id=self.manifest.experiment_id)
        ref = cp.CurrentRef(
            experiment_id=self.manifest.experiment_id, checkpoint=final_name,
            checkpoint_manifest_sha256=cp_manifest_sha, boundary_epoch=epoch,
            snapshot_sha256=snapshot.sha256, prior_checkpoint=prior,
            dry_run=bool(dry_run),
            published_utc=now.astimezone(timezone.utc).isoformat())
        self._swing_current(ref, prior)
        return final_name

    def _fetch(self) -> Callable[..., object]:
        if self._fetch_ohlcv is not None:
            return self._fetch_ohlcv
        # Default: Coinbase public keyless candles (READ-ONLY market data).
        from ..data.public import PublicMarketData
        return PublicMarketData().fetch_ohlcv
