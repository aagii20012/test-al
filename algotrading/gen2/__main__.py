"""Generation-2 coordinator runner — INERT BY DEFAULT.

    python -m algotrading.gen2 <command> [options]

This is the human-operated entry point for a Generation-2 experiment. Importing
this module does NOTHING; only an explicit subcommand acts, and the two commands
that could ever put real (paper) money to work are hard-gated behind an
"I understand this goes live" flag AND, underneath, the coordinator's own
approval gate. There is no scheduler here and no network call except the
read-only public-candle fetch a tick performs.

Commands, from safest to most consequential:

  prepare     Build an immutable experiment manifest bound to the CURRENT code
              + config and write the PREPARED scaffolding under
              state/gen2/<experiment_id>/. Does not trade, does not fetch. Safe
              to run repeatedly (refuses to clobber an existing experiment).

  scoreboard  Render the offline HTML scoreboard for an experiment. Read-only.

  preflight   Run the keyless public market-data provenance gate against the live
              Coinbase endpoint (launch/strict mode) and print the report. Makes a
              read-only HTTPS request; places no order. Exit 0 = certified,
              2 = provenance failed, 3 = network blocked. Read-only w.r.t. state
              (optionally records the hashed report under the experiment dir).

  verify-current  Resolve the single CURRENT pointer and re-verify the checkpoint
              manifest + EVERY artifact hash, then print what a reader/dashboard
              would see. Read-only; fails closed (exit 2) on any integrity defect.

  dry-run     Rehearse ONE coordinator tick end-to-end (fetch -> snapshot ->
              run all 8 bots -> publish), but marked dry_run. A dry-run NEVER
              flips the board to "trading" and NEVER requires activation; it is
              the safe way to prove the machinery works on real candles.

  activate    Flip an experiment PREPARED -> ACTIVE. This is the launch switch.
              Refuses without --i-understand-this-goes-live, and the coordinator
              refuses unless approved=True is threaded through — so a stray
              invocation cannot start live paper trading. Immediately before the
              flip it runs the keyless public provenance gate (strict) and the
              injected-fetch guard, and STOPS (BLOCKED / provenance-failed) rather
              than launch on data it cannot prove is the genuine fresh feed.

  tick        Run ONE *live* (non-dry-run) coordinator tick. Requires the
              experiment to already be ACTIVE and --i-understand-this-goes-live.
              This is what a scheduler would call once the experiment is live.

Everything is confined to state/gen2/<experiment_id>/; no Generation-1 file is
ever read or written.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

from .. import state_schema
from . import checkpoint as cp
from . import dashboard
from . import provenance as prov
from .checkpoint import CheckpointError
from .coordinator import Gen2Coordinator, NotActivatedError
from .experiment import (
    RETIRED_EXPERIMENT_IDS, ExperimentManifest, Status, build_manifest)

_DEFAULT_STATE_ROOT = "state"
_DEFAULT_CONFIG = "config/config.ci.yaml"


# --------------------------------------------------------------------------
# locating an already-prepared experiment
# --------------------------------------------------------------------------
def _gen2_root(state_root: str) -> str:
    return os.path.join(state_root, state_schema.GENERATION)


def _discover_experiment_id(state_root: str) -> str:
    """Find THE single prepared experiment, or fail loudly.

    Prevents a command from silently acting on the wrong experiment when more
    than one exists — the operator must then name it with --experiment-id.
    """
    pattern = os.path.join(_gen2_root(state_root), "*", "manifest.json")
    ids = sorted(
        os.path.basename(os.path.dirname(p)) for p in glob.glob(pattern))
    if not ids:
        raise SystemExit(
            f"No prepared Generation-2 experiment under {_gen2_root(state_root)!r}. "
            "Run `python -m algotrading.gen2 prepare` first.")
    if len(ids) > 1:
        raise SystemExit(
            "Multiple experiments found; pass --experiment-id to choose one:\n  "
            + "\n  ".join(ids))
    return ids[0]


def _has_single_experiment(state_root: str) -> bool:
    """True iff exactly one prepared experiment exists (never raises)."""
    pattern = os.path.join(_gen2_root(state_root), "*", "manifest.json")
    return len(glob.glob(pattern)) == 1


def _deployable_experiment_id(state_root: str):
    """Return THE single deployable (non-terminal, non-retired) experiment id.

    The public dashboard must never surface a retired/terminal experiment as if it
    were the live board. This filters those out and returns:
      * the id            when exactly one deployable experiment exists;
      * None              when none exist (dormant phase -> placeholder page);
    and raises SystemExit when more than one is deployable (ambiguous — the
    operator must pass --experiment-id explicitly). It never raises just because a
    retired experiment is also present on disk.
    """
    pattern = os.path.join(_gen2_root(state_root), "*", "manifest.json")
    deployable = []
    for path in sorted(glob.glob(pattern)):
        with open(path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        exp_id = m.get("experiment_id")
        status = m.get("status")
        if exp_id in RETIRED_EXPERIMENT_IDS or status in Status.TERMINAL:
            continue
        deployable.append(exp_id)
    if not deployable:
        return None
    if len(deployable) > 1:
        raise SystemExit(
            "More than one deployable Generation-2 experiment; pass "
            "--experiment-id to choose one:\n  " + "\n  ".join(deployable))
    return deployable[0]


def _load_coord(args, *, allow_fresh: bool) -> Gen2Coordinator:
    """Reconstruct a coordinator around an existing on-disk manifest."""
    state_root = args.state_root
    exp_id = args.experiment_id or _discover_experiment_id(state_root)
    manifest_path = os.path.join(_gen2_root(state_root), exp_id, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit(f"No manifest at {manifest_path!r}.")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = ExperimentManifest.from_dict(json.load(fh))
    return Gen2Coordinator(
        manifest, state_root=state_root, config_path=args.config,
        allow_fresh=allow_fresh, verify_code=not args.no_verify_code)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_prepare(args) -> int:
    manifest = build_manifest(
        created=datetime.now(timezone.utc), config_path=args.config,
        implementation_commit=args.implementation_commit)
    coord = Gen2Coordinator(
        manifest, state_root=args.state_root, config_path=args.config,
        verify_code=not args.no_verify_code)
    path = coord.prepare()
    code = manifest.code
    tree = code.get("source_tree_sha256") or ""
    print(f"PREPARED experiment {manifest.experiment_id}")
    print(f"  manifest : {path}")
    print(f"  dir      : {coord.exp_dir}")
    print(f"  status   : {manifest.status} (not trading; activation is separate)")
    print(f"  bots     : {len(manifest.bots)} @ ${manifest.capital_per_bot:,.0f} each")
    print(f"  impl. commit : {code.get('implementation_commit') or 'n/a'}")
    print(f"  source tree  : {tree[:16]} ({code.get('source_file_count')} *.py files)")
    print(f"  config sha   : {(manifest.config.get('sha256') or '')[:16]}")
    print(f"  binding sha  : {manifest.binding_sha256[:16]}")
    if args.implementation_commit is None:
        print("  NOTE: no --implementation-commit given; bound to local HEAD as a "
              "best-effort marker. For a two-stage launch, pass the pushed Stage-A "
              "commit so the binding refers to a commit that exists on the remote.")
    return 0


def cmd_scoreboard(args) -> int:
    exp_id = args.experiment_id or _discover_experiment_id(args.state_root)
    exp_dir = os.path.join(_gen2_root(args.state_root), exp_id)
    sb = dashboard.build_scoreboard(exp_dir)
    html = dashboard.render_html(sb)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"Scoreboard written to {args.out} "
              f"(status={sb['status']}, trading={sb['trading']})")
    else:
        sys.stdout.write(html)
    return 0


def cmd_build_pages(args) -> int:
    """Build the deployed GitHub Pages site into --out (default: site/).

    Selects the single deployable experiment (or honours --experiment-id). When
    none is deployable (only a retired/terminal experiment on disk), it writes an
    honest placeholder page instead of failing, so the public site never implies a
    board is live. A corrupt/unverifiable CURRENT for the selected experiment
    fails closed (build_scoreboard raises) so the deploy step is skipped and the
    last-good published site is preserved.
    """
    out_dir = args.out
    exp_id = args.experiment_id or _deployable_experiment_id(args.state_root)
    if exp_id is None:
        os.makedirs(out_dir, exist_ok=True)
        html = dashboard.render_pages_placeholder_html()
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(html)
        with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("")
        print(f"Pages site written to {out_dir} (no deployable experiment; "
              "placeholder page)")
        return 0
    exp_dir = os.path.join(_gen2_root(args.state_root), exp_id)
    sb = dashboard.build_pages_site(exp_dir, out_dir)
    print(f"Pages site written to {out_dir} "
          f"(experiment={exp_id}, status={sb['status']}, trading={sb['trading']})")
    return 0


def _run_provenance_gate(*, tolerance_s: int, now=None) -> prov.ProvenanceReport:
    """Perform the genuine keyless public preflight (strict/launch mode).

    ``strict=True`` means the gate makes its OWN real ``requests.get`` — it will
    NOT accept an injected fetch — and additionally proves the requests library
    and ``PublicMarketData.fetch_ohlcv`` are un-monkeypatched.
    """
    return prov.preflight(
        prov.REQUIRED_PRODUCTS, freshness_tolerance_s=tolerance_s,
        strict=True, now=now)


def _print_provenance(report: prov.ProvenanceReport) -> None:
    print(f"provenance preflight ({report.source}, strict={report.strict}) "
          f"@ {report.checked_utc}")
    print(f"  interval={report.interval}  freshness_tol={report.freshness_tolerance_s}s"
          f"  min_warmup={report.min_warmup}")
    print(f"  shared_boundary_epoch_ms={report.shared_boundary_epoch_ms}")
    for p in report.products:
        print(f"  {p.symbol:<8} {p.product:<8} ok={p.ok} status={p.http_status} "
              f"host={p.url.split('/')[2]}")
        print(f"      closed_candles={p.candle_count} newest_close_age_s="
              f"{p.newest_close_age_s} price={p.newest_price} vol={p.newest_volume}")
        print(f"      server_date={p.server_date_utc} raw_sha256={p.raw_sha256} "
              f"raw_bytes={p.raw_bytes}")
        for issue in p.issues:
            print(f"      ISSUE {issue}")
    for issue in report.issues:
        print(f"  ISSUE {issue}")


def _persist_provenance(exp_dir: str, report: prov.ProvenanceReport) -> str:
    """Write the report (hashed by content) under <exp_dir>/preflight/ and return
    the path. Preserving the raw report makes the exact market bytes the launch
    decision rested on auditable after the fact. Never touches CURRENT or state."""
    payload = report.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    from .experiment import sha256_bytes
    digest = sha256_bytes(text.encode("utf-8"))[:16]
    out_dir = os.path.join(exp_dir, "preflight")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"provenance-{digest}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cmd_preflight(args) -> int:
    report = _run_provenance_gate(tolerance_s=args.tolerance)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        _print_provenance(report)
    if args.experiment_id or _has_single_experiment(args.state_root):
        try:
            exp_id = args.experiment_id or _discover_experiment_id(args.state_root)
            exp_dir = os.path.join(_gen2_root(args.state_root), exp_id)
            if os.path.isdir(exp_dir):
                print(f"  report saved: {_persist_provenance(exp_dir, report)}")
        except SystemExit:
            pass
    if report.blocked:
        print("BLOCKED: market-data endpoint unreachable — refusing to certify a "
              "feed on uncertain data.")
        return 3
    if report.failed:
        print("MARKET_DATA_PROVENANCE_FAILED: the feed could not be certified.")
        return 2
    print("PROVENANCE OK: the live feed is certified for launch.")
    return 0


def cmd_verify_current(args) -> int:
    coord = _load_coord(args, allow_fresh=True)
    ref = coord.read_current()
    if ref is None:
        print(f"{coord.manifest.experiment_id}: no published checkpoint "
              "(CURRENT is absent). Nothing to verify.")
        return 0
    try:
        # resolve_current re-verifies the checkpoint manifest AND every artifact
        # hash against CURRENT; a mismatch raises (fail closed).
        ck = coord.resolve_current()
    except CheckpointError as e:
        print(f"INTEGRITY FAILURE: {e}")
        return 2
    assert ck is not None
    print(f"CURRENT verified for {coord.manifest.experiment_id}")
    print(f"  checkpoint      : {ref.checkpoint}")
    print(f"  manifest sha256 : {ref.checkpoint_manifest_sha256}")
    print(f"  boundary epoch  : {ref.boundary_epoch}")
    print(f"  snapshot sha256 : {ref.snapshot_sha256}")
    print(f"  prior checkpoint: {ref.prior_checkpoint}")
    print(f"  dry_run         : {ref.dry_run}")
    print(f"  published_utc   : {ref.published_utc}")
    print(f"  bots verified   : {len(ck.bot_states)}")
    return 0


def cmd_dry_run(args) -> int:
    coord = _load_coord(args, allow_fresh=True)
    result = coord.run_tick(dry_run=True)
    print(f"DRY-RUN {result.status} for {result.experiment_id}")
    print(f"  decision epoch : {result.decision_epoch_ms}")
    print(f"  snapshot sha256: {result.snapshot_sha256}")
    print(f"  bots           : {len(result.bots)} (dry_run={result.dry_run}; "
          "scoreboard stays 'not trading')")
    return 0


def cmd_activate(args) -> int:
    if not args.i_understand_this_goes_live:
        raise SystemExit(
            "Refusing to ACTIVATE without --i-understand-this-goes-live. "
            "Activation starts live paper trading and is a human-gated launch step.")
    coord = _load_coord(args, allow_fresh=False)

    # The coordinator that will tick must consume the GENUINE public feed, not an
    # injected one. (A CLI-loaded coordinator never has an injected fetch, but this
    # keeps the guarantee explicit and covers programmatic callers.)
    prov.assert_live_fetch_only(coord)

    # Launch-time provenance gate: prove — via the gate's OWN keyless request,
    # right now, immediately before the flip — that the feed is the intended, fresh
    # Coinbase public data. STOP rather than launch on data we cannot certify.
    print("Running launch-time market-data provenance gate (keyless, read-only)...")
    report = _run_provenance_gate(tolerance_s=args.tolerance)
    _print_provenance(report)
    print(f"  report saved: {_persist_provenance(coord.exp_dir, report)}")
    if report.blocked:
        raise SystemExit(
            "BLOCKED: the Coinbase public endpoint could not be reached, so the "
            "feed cannot be certified. Refusing to activate on uncertain data.")
    if report.failed:
        raise SystemExit(
            "MARKET_DATA_PROVENANCE_FAILED: the feed did not certify. Refusing to "
            "activate. See the issues above.")

    coord.set_status(Status.ACTIVE, approved=True)
    print(f"ACTIVATED {coord.manifest.experiment_id} -> {coord.manifest.status}")
    print("  The experiment will now accept live (non-dry-run) ticks.")
    return 0


def cmd_tick(args) -> int:
    if not args.i_understand_this_goes_live:
        raise SystemExit(
            "Refusing a LIVE tick without --i-understand-this-goes-live. "
            "A live tick publishes real paper-trading results.")
    coord = _load_coord(args, allow_fresh=args.allow_fresh)
    try:
        result = coord.run_tick(dry_run=False)
    except NotActivatedError as e:
        raise SystemExit(f"Cannot tick: {e}")
    print(f"LIVE {result.status} for {result.experiment_id}")
    print(f"  decision epoch : {result.decision_epoch_ms}")
    print(f"  snapshot sha256: {result.snapshot_sha256}")
    for b in result.bots:
        print(f"    {b['bot_id']:<22} equity={b['equity']:,.2f} "
              f"pos={b['position']:.4f} acted={b['acted']}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m algotrading.gen2",
        description="Generation-2 coordinator runner (inert by default).")
    p.add_argument("--state-root", dest="state_root", default=_DEFAULT_STATE_ROOT,
                   help="root under which state/gen2/<id>/ lives (default: state)")
    p.add_argument("--config", default=_DEFAULT_CONFIG,
                   help=f"config path (default: {_DEFAULT_CONFIG})")
    p.add_argument("--experiment-id", dest="experiment_id", default=None,
                   help="target experiment id (auto-discovered if only one exists)")
    p.add_argument("--no-verify-code", dest="no_verify_code", action="store_true",
                   help="skip the code-drift binding check (testing only)")
    sub = p.add_subparsers(dest="command", required=True)

    pp = sub.add_parser(
        "prepare",
        help="build + write a PREPARED experiment (no trading, no fetch)")
    pp.add_argument("--implementation-commit", dest="implementation_commit",
                    default=None,
                    help="the already-pushed Stage-A commit whose tree the binding "
                         "is computed from (default: local HEAD, best-effort)")

    ps = sub.add_parser("scoreboard", help="render the offline HTML scoreboard")
    ps.add_argument("--out", default=None,
                    help="write HTML here (default: print to stdout)")

    pg = sub.add_parser(
        "build-pages",
        help="build the deployed GitHub Pages site (index.html + .nojekyll)")
    pg.add_argument("--out", default="site",
                    help="output directory for the static site (default: site)")

    pf = sub.add_parser(
        "preflight",
        help="run the keyless public market-data provenance gate (read-only)")
    pf.add_argument("--tolerance", type=int, default=prov.DEFAULT_FRESHNESS_TOLERANCE_S,
                    help="freshness tolerance in seconds "
                         f"(default: {prov.DEFAULT_FRESHNESS_TOLERANCE_S})")
    pf.add_argument("--json", action="store_true",
                    help="emit the full report as JSON instead of a summary")

    sub.add_parser(
        "verify-current",
        help="resolve CURRENT and re-verify the checkpoint + all hashes (read-only)")

    sub.add_parser(
        "dry-run",
        help="rehearse ONE tick (marked dry_run; never flips to trading)")

    pa = sub.add_parser("activate", help="flip PREPARED -> ACTIVE (launch switch)")
    pa.add_argument("--i-understand-this-goes-live",
                    dest="i_understand_this_goes_live", action="store_true",
                    help="required: activation starts live paper trading")
    pa.add_argument("--tolerance", type=int, default=prov.DEFAULT_FRESHNESS_TOLERANCE_S,
                    help="freshness tolerance for the launch provenance gate "
                         f"(default: {prov.DEFAULT_FRESHNESS_TOLERANCE_S})")

    pt = sub.add_parser("tick", help="run ONE live (non-dry-run) tick; requires ACTIVE")
    pt.add_argument("--i-understand-this-goes-live",
                    dest="i_understand_this_goes_live", action="store_true",
                    help="required: a live tick publishes real paper-trading results")
    pt.add_argument("--allow-fresh", dest="allow_fresh", action="store_true",
                    help="permit a fresh first tick when no prior bot state exists")

    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    handlers = {
        "prepare": cmd_prepare,
        "scoreboard": cmd_scoreboard,
        "build-pages": cmd_build_pages,
        "preflight": cmd_preflight,
        "verify-current": cmd_verify_current,
        "dry-run": cmd_dry_run,
        "activate": cmd_activate,
        "tick": cmd_tick,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
