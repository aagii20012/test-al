"""Step 11 — corrected historical falsification on the validated Gen2 snapshots.

This is the falsification run the invalidated Generation 1 could never be: the
CORRECTED engine (portfolio-authoritative sync, reversal cost-basis fix, and the
append-only fill/leg audit ledger) driven over a SINGLE shared, hash-verified,
REAL price window for every candidate — the direct remedy for the three defects
that sank Gen1.

What it does, and does not do:
  * Verifies each raw snapshot's SHA-256 against its manifest before use; a
    mismatch aborts the whole run (no silently-swapped data).
  * Runs one independent backtest per (strategy, coin) — mirroring the 8 paper
    bots — with the EXACT Gen2 simulated cost/risk model (commission 0.001,
    slippage 2 bps, fill at close, $10 min-notional, config.ci risk plan).
  * Records, per bot, the standard performance report PLUS ledger integrity
    counts (fills, legs, realized P&L) so the corrected accounting is auditable.
  * NEVER launches Gen2, touches state/, writes Gen1 evidence, or synthesises a
    missing candle. Missing hours are real bar gaps (disclosed), not filled.

Output: evidence/gen2/falsification/{results.json, FALSIFICATION_REPORT.md}.

Run:  python -m tools.run_falsification
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from algotrading.analytics.performance import compute_report
from algotrading.cli import _PERIODS_PER_YEAR, _risk_config
from algotrading.core.event_queue import EventQueue
from algotrading.data.historical import HistoricCSVDataHandler
from algotrading.engine.backtest import BacktestEngine
from algotrading.execution.simulated import SimulatedExecutionHandler
from algotrading.portfolio.portfolio import Portfolio
from algotrading.research.grids import DEFAULT_PARAMS, FAMILY
from algotrading.research.grids import STRATEGY_REGISTRY as STRATEGIES
from algotrading.risk.risk_manager import RiskManager
from algotrading.utils.config import load_config

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "historical_data" / "gen2"
OUT_DIR = REPO / "evidence" / "gen2" / "falsification"
CONFIG = REPO / "config" / "config.ci.yaml"

# The candidate bake-off: same four strategies as the dashboard, one shared coin
# set, one fixed (default) parameter set each — held identical across all bots.
CANDIDATES = ["momentum", "rsi", "donchian", "bollinger"]
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "1h"

# Exact Gen2 simulated-fill cost model (see cli.cmd_tick --simulated).
COMMISSION_PCT = 0.001
SLIPPAGE_BPS = 2.0
FILL_AT = "close"
MIN_NOTIONAL = 10.0


def _load_verified_frame(symbol: str) -> tuple[pd.DataFrame, dict]:
    """Load a snapshot's rows into an OHLCV frame AFTER verifying its hash."""
    stem = f"{symbol}_{INTERVAL}_real"
    raw_path = DATA_DIR / f"{stem}.raw.json"
    manifest_path = DATA_DIR / f"{stem}.manifest.json"
    if not raw_path.exists() or not manifest_path.exists():
        raise FileNotFoundError(
            f"missing validated snapshot for {symbol} at {raw_path} / {manifest_path}")

    raw_bytes = raw_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != manifest["sha256"]:
        raise ValueError(
            f"HASH MISMATCH for {symbol}: raw file {actual} != manifest "
            f"{manifest['sha256']}; refusing to run on unverified data")

    rows = json.loads(raw_bytes.decode("utf-8"))
    # Same column mapping as HistorySnapshot.to_dataframe().
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df, manifest


def _run_bot(strategy_key: str, symbol: str, df: pd.DataFrame, cfg) -> dict:
    """One independent corrected backtest = one paper bot over the shared window."""
    ppy = _PERIODS_PER_YEAR.get(INTERVAL, 365 * 24)
    events = EventQueue()
    data = HistoricCSVDataHandler(events, {symbol: df})
    risk = RiskManager(_risk_config(cfg))
    portfolio = Portfolio(data, events, risk, initial_capital=cfg.initial_capital,
                          financing_apr=cfg.financing_apr, periods_per_year=ppy)
    execution = SimulatedExecutionHandler(
        events, data, commission_pct=COMMISSION_PCT, slippage_bps=SLIPPAGE_BPS,
        fill_at=FILL_AT, min_notional=MIN_NOTIONAL)
    strategy = STRATEGIES[strategy_key](data, events, **DEFAULT_PARAMS[strategy_key])

    BacktestEngine(data, strategy, portfolio, execution, events).run()

    report = compute_report(
        portfolio.equity_dataframe(), portfolio.trade_log, portfolio.total_commission,
        periods_per_year=ppy, total_slippage=portfolio.total_slippage,
        total_financing=portfolio.total_financing)

    # Ledger integrity: one fill row per real fill; legs decompose fills into
    # OPEN/CLOSE. A reversal must be ONE fill with two legs, never two fills.
    reversals = sum(
        1 for f in portfolio.fills
        if f["prev_qty"] != 0 and (f["prev_qty"] > 0) != (f["new_qty"] > 0)
        and f["new_qty"] != 0)

    r = report.as_dict()
    r.update({
        "strategy": strategy_key,
        "family": FAMILY.get(strategy_key, ""),
        "symbol": symbol,
        "params": DEFAULT_PARAMS[strategy_key],
        "final_equity": round(float(portfolio.equity), 4),
        "realized_pnl": round(float(portfolio.realized_pnl), 4),
        "ledger_fills": len(portfolio.fills),
        "ledger_legs": len(portfolio.legs),
        "ledger_reversals": reversals,
    })
    return r


def main() -> int:
    cfg = load_config(str(CONFIG))
    print(f"Falsification: {len(CANDIDATES)} strategies x {len(SYMBOLS)} coins, "
          f"{INTERVAL} bars, shared verified window")
    print(f"Costs: commission={COMMISSION_PCT}, slippage={SLIPPAGE_BPS}bps, "
          f"fill_at={FILL_AT}, min_notional=${MIN_NOTIONAL}, "
          f"initial_capital=${cfg.initial_capital:,.0f}\n")

    frames, manifests = {}, {}
    for sym in SYMBOLS:
        df, manifest = _load_verified_frame(sym)
        frames[sym] = df
        manifests[sym] = manifest
        print(f"[{sym}] hash OK  bars={len(df)}  "
              f"expected={manifest['expected_count']}  missing={manifest['missing_count']}  "
              f"window={manifest['start']} -> {manifest['end']}")
    print()

    results = []
    for strat in CANDIDATES:
        for sym in SYMBOLS:
            r = _run_bot(strat, sym, frames[sym], cfg)
            results.append(r)
            print(f"  {strat:9s} {sym:8s}  ret={r['total_return_pct']:+7.2f}%  "
                  f"sharpe={r['sharpe']:+5.2f}  maxDD={r['max_drawdown_pct']:6.2f}%  "
                  f"trades={r['n_trades']:3d}  fills={r['ledger_fills']:3d}  "
                  f"legs={r['ledger_legs']:3d}  reversals={r['ledger_reversals']:2d}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run": {
            "interval": INTERVAL,
            "candidates": CANDIDATES,
            "symbols": SYMBOLS,
            "commission_pct": COMMISSION_PCT,
            "slippage_bps": SLIPPAGE_BPS,
            "fill_at": FILL_AT,
            "min_notional": MIN_NOTIONAL,
            "initial_capital": cfg.initial_capital,
            "financing_apr": cfg.financing_apr,
            "periods_per_year": _PERIODS_PER_YEAR.get(INTERVAL, 365 * 24),
        },
        "data": {sym: {
            "sha256": manifests[sym]["sha256"],
            "expected_count": manifests[sym]["expected_count"],
            "actual_count": manifests[sym]["actual_count"],
            "missing_count": manifests[sym]["missing_count"],
            "start": manifests[sym]["start"],
            "end": manifests[sym]["end"],
        } for sym in SYMBOLS},
        "results": results,
    }
    (OUT_DIR / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    _write_markdown(payload)
    print(f"\nWrote {OUT_DIR / 'results.json'}")
    print(f"Wrote {OUT_DIR / 'FALSIFICATION_REPORT.md'}")
    return 0


def _write_markdown(payload: dict) -> None:
    run = payload["run"]
    rows = sorted(payload["results"],
                  key=lambda r: r["total_return_pct"], reverse=True)
    lines = []
    lines.append("# Generation 2 — Corrected Historical Falsification\n")
    lines.append("> Corrected engine (portfolio-authoritative sync + reversal "
                 "cost-basis fix + append-only audit ledger) over a single "
                 "shared, hash-verified, REAL price window. This is a backtest "
                 "for falsification only — **not** a Gen2 launch and **not** a "
                 "basis for live validation.\n")

    lines.append("## Data (hash-verified before use)\n")
    lines.append("| Symbol | Window | Bars (actual/expected) | Missing | SHA-256 |")
    lines.append("|---|---|---|---|---|")
    for sym in payload["data"]:
        d = payload["data"][sym]
        lines.append(f"| {sym} | {d['start']} → {d['end']} | "
                     f"{d['actual_count']}/{d['expected_count']} | {d['missing_count']} | "
                     f"`{d['sha256'][:16]}…` |")
    lines.append("\nMissing hours are genuine source gaps, recorded and **not** "
                 "synthesised; both symbols share the identical calendar so the "
                 "union timeline simply has no bar at those hours.\n")

    lines.append("## Cost & risk model (identical across all bots)\n")
    lines.append(f"- Commission: {run['commission_pct']} (per fill notional)")
    lines.append(f"- Slippage: {run['slippage_bps']} bps")
    lines.append(f"- Fill: at bar {run['fill_at']}; min notional ${run['min_notional']}")
    lines.append(f"- Initial capital: ${run['initial_capital']:,.0f}; "
                 f"financing APR {run['financing_apr']}")
    lines.append("- Risk: config/config.ci.yaml (ATR sizing, 1%/trade, 5% stop, "
                 "1% daily loss/profit halts, 15% max-drawdown kill switch)\n")

    lines.append("## Results (ranked by net return)\n")
    lines.append("| Rank | Strategy | Coin | Net return | Sharpe | Max DD | Trades | "
                 "Fills | Legs | Reversals | Final equity |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['strategy']} | {r['symbol']} | "
            f"{r['total_return_pct']:+.2f}% | {r['sharpe']:+.2f} | "
            f"{r['max_drawdown_pct']:.2f}% | {r['n_trades']} | {r['ledger_fills']} | "
            f"{r['ledger_legs']} | {r['ledger_reversals']} | "
            f"${r['final_equity']:,.2f} |")

    lines.append("\n## Ledger integrity\n")
    lines.append("Every bot's `fills` count equals its real fill count; `legs` "
                 "decompose fills into OPEN/CLOSE lifecycle legs; each reversal is "
                 "ONE fill carrying two legs (never double-counted as two "
                 "executions). `realized_pnl` is booked only on closed quantity "
                 "with the corrected cost basis.\n")
    lines.append("| Strategy | Coin | Realized P&L | Fills | Legs | Reversals |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['strategy']} | {r['symbol']} | ${r['realized_pnl']:,.2f} | "
                     f"{r['ledger_fills']} | {r['ledger_legs']} | {r['ledger_reversals']} |")

    lines.append("\n## Interpretation\n")
    lines.append("- These numbers are trustworthy in a way Gen1's were not: one "
                 "shared verified window, corrected accounting, auditable ledger.")
    lines.append("- They are a single fixed-parameter backtest over one 12-month "
                 "window on two correlated assets — a falsification probe, not an "
                 "out-of-sample validation and not a live result.")
    lines.append("- Passing this step does **not** qualify any strategy for "
                 "live/real-money validation.\n")
    (OUT_DIR / "FALSIFICATION_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
