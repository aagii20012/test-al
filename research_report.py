#!/usr/bin/env python
"""End-to-end strategy evaluation and the 10%-per-day reality check.

What this script does, all on the SAME engine that backtest and live trading use:

  1. Regime stress test — run every strategy family (trend / mean-reversion /
     breakout / momentum / benchmark) across four synthetic regimes (bull, bear,
     chop, crash) plus the real BTCUSDT hourly history, under a strict,
     realistic risk policy with fees and slippage.
  2. Walk-forward (out-of-sample) — for each family, re-optimise parameters on a
     rolling training window and trade them on the next, unseen window. The
     stitched test curve is genuine out-of-sample performance.
  3. Verdict — compare the best honest out-of-sample result against the 10%/day
     target and report whether it is reachable, plus the recommended strategy.

Outputs:
  * console tables,
  * reports/strategy_evaluation.md   (full written report),
  * reports/oos_<best>.html          (standalone OOS dashboard).

Run:  python research_report.py            (uses cached real data if present)
      python research_report.py --fast     (smaller grids / windows, quicker)
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, List, Optional

import pandas as pd

from algotrading.analytics.dashboard import generate_html_report
from algotrading.analytics.performance import PerformanceReport
from algotrading.research.grids import (
    DEFAULT_PARAMS, FAMILY, PARAM_GRIDS, STRATEGY_REGISTRY,
)
from algotrading.research.regimes import REGIMES, make_regime_frames
from algotrading.research.runner import run_backtest
from algotrading.research.walkforward import walk_forward
from algotrading.risk.risk_manager import RiskConfig

# --------------------------------------------------------------------------
# Realistic, conservative assumptions
# --------------------------------------------------------------------------
COMMISSION_PCT = 0.001   # 0.10% taker fee per side (Binance spot worst case)
SLIPPAGE_BPS = 2.0       # 2 bp adverse slippage per fill
INITIAL_CAPITAL = 100_000.0
PERIODS_PER_YEAR = 365 * 24  # hourly bars
DATA_PATH = os.path.join("data_cache", "BTCUSDT_1h.csv")
OUT_DIR = "reports"

# A strict production risk policy. Volatility (ATR) position sizing risks a fixed
# fraction of equity to a volatility-scaled stop; portfolio circuit breakers cap
# the daily loss and the peak-to-trough drawdown.
MANAGED_RISK = RiskConfig(
    max_position_pct=0.50,    # never more than 50% of equity in one symbol
    risk_per_trade=0.02,      # risk 2% of equity to the stop on each trade
    max_leverage=1.0,         # spot, no leverage
    allow_short=True,
    use_stops=True,
    stop_loss_pct=0.05,       # fallback stop if ATR is unavailable
    atr_sizing=True,
    atr_period=14,
    atr_stop_mult=2.5,        # stop 2.5 ATR away from entry
    max_daily_loss_pct=0.05,  # halt & flatten for the day at -5%
    max_drawdown_pct=0.25,    # kill switch: halt permanently at -25% drawdown
)


def _row(name: str, r: PerformanceReport) -> dict:
    pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
    return {
        "strategy": name,
        "tot_ret_%": round(r.total_return_pct, 2),
        "avg_day_%": round(r.avg_daily_return_pct, 4),
        "sharpe": round(r.sharpe, 2),
        "sortino": round(r.sortino, 2),
        "maxDD_%": round(r.max_drawdown_pct, 2),
        "calmar": round(r.calmar, 2),
        "win_%": round(r.win_rate_pct, 1),
        "PF": pf,
        "trades": r.n_trades,
    }


def _print_table(title: str, rows: List[dict]) -> str:
    df = pd.DataFrame(rows)
    block = f"\n### {title}\n\n" + df.to_markdown(index=False)
    print(f"\n=== {title} ===")
    print(df.to_string(index=False))
    return block


# --------------------------------------------------------------------------
def load_real_frames() -> Optional[Dict[str, pd.DataFrame]]:
    if not os.path.exists(DATA_PATH):
        return None
    df = pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)
    return {"BTCUSDT": df}


# --------------------------------------------------------------------------
def regime_stress_test(strategies: List[str]) -> str:
    """One default-parameter run per (strategy, regime). No optimisation —
    just: does each family behave the way its theory predicts across regimes?"""
    datasets: Dict[str, Dict[str, pd.DataFrame]] = {
        r: make_regime_frames(r, ["BTCUSDT"], n_bars=4000, seed=11) for r in REGIMES
    }
    real = load_real_frames()
    if real is not None:
        datasets["real_BTC"] = real

    md = ["\n## 1. Regime stress test (default params, managed risk, with costs)\n"]
    for ds_name, frames in datasets.items():
        rows = []
        for key in strategies:
            res = run_backtest(
                frames, STRATEGY_REGISTRY[key], DEFAULT_PARAMS.get(key, {}), MANAGED_RISK,
                initial_capital=INITIAL_CAPITAL, commission_pct=COMMISSION_PCT,
                slippage_bps=SLIPPAGE_BPS, periods_per_year=PERIODS_PER_YEAR,
            )
            rows.append(_row(f"{key} ({FAMILY[key]})", res.report))
        md.append(_print_table(f"Regime: {ds_name}", rows))
    return "\n".join(md)


def walk_forward_eval(strategies: List[str], train: int, test: int):
    """Walk-forward OOS on the real BTC history for every strategy."""
    real = load_real_frames()
    if real is None:
        print("No cached real data; generating a synthetic 'real-like' series.")
        real = make_regime_frames("bull", ["BTCUSDT"], n_bars=8760, seed=3)

    results = {}
    rows = []
    for key in strategies:
        wf = walk_forward(
            real, STRATEGY_REGISTRY[key], PARAM_GRIDS.get(key, {}), MANAGED_RISK,
            train_bars=train, test_bars=test, objective="sharpe",
            commission_pct=COMMISSION_PCT, slippage_bps=SLIPPAGE_BPS,
            initial_capital=INITIAL_CAPITAL, periods_per_year=PERIODS_PER_YEAR,
        )
        results[key] = wf
        rows.append({**_row(f"{key} ({FAMILY[key]})", wf.oos_report),
                     "windows": wf.n_windows})
    md = "\n## 2. Walk-forward OUT-OF-SAMPLE on real BTCUSDT 1h\n"
    md += _print_table("Out-of-sample (stitched test windows)", rows)
    return results, md


def verdict(best_key: str, best_wf) -> str:
    r = best_wf.oos_report
    daily = r.avg_daily_return_pct / 100.0
    target = 0.10
    # What 10%/day implies, compounded:
    yr_252 = (1 + target) ** 252
    yr_365 = (1 + target) ** 365
    best_yr = (1 + daily) ** 365 if daily > -1 else 0.0

    md = ["\n## 3. Verdict — is 10% average daily return achievable?\n"]
    md.append("**No. Not within several orders of magnitude, and not by any "
              "strategy — this is arithmetic, not pessimism.**\n")
    md.append("A sustained 10% *daily* return compounds to:\n")
    md.append(f"- `(1.10)^252  ≈ {yr_252:,.0f}×` starting capital in one trading year")
    md.append(f"- `(1.10)^365  ≈ {yr_365:,.3e}×` over a calendar year")
    md.append(f"- ${INITIAL_CAPITAL:,.0f} would become "
              f"**${INITIAL_CAPITAL * yr_252:,.3e}** in ~12 months\n")
    md.append("That exceeds the market cap of every crypto asset combined within "
              "weeks, so it cannot persist: your own orders would move the market "
              "long before then. For reference, the best track records in history "
              "(Renaissance Medallion ≈ 66%/yr, Buffett ≈ 20%/yr) correspond to "
              "roughly **0.05–0.2% per day**.\n")
    md.append("### What this framework actually achieves (honest, out-of-sample)\n")
    md.append(f"- Best risk-adjusted strategy: **{best_key} ({FAMILY[best_key]})**")
    md.append(f"- OOS average daily return: **{r.avg_daily_return_pct:.4f}%/day** "
              f"(≈ {best_yr - 1:+.1%}/yr if it held)")
    md.append(f"- OOS Sharpe: **{r.sharpe:.2f}**, Sortino: {r.sortino:.2f}, "
              f"max drawdown: {r.max_drawdown_pct:.1f}%, Calmar: {r.calmar:.2f}")
    pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
    md.append(f"- Profit factor: {pf}, win rate: {r.win_rate_pct:.1f}%, "
              f"trades: {r.n_trades}\n")
    gap = target / daily if daily > 0 else float("inf")
    if daily > 0:
        md.append(f"The 10%/day target is **~{gap:,.0f}× larger** than the best "
                  "honest daily return found here.\n")
    else:
        md.append("Even the best out-of-sample strategy did not produce a positive "
                  "average daily return over this period — the 10%/day target is "
                  "not merely unreachable, the realistic expectation is near zero "
                  "or negative for a naive single-asset crypto system after costs.\n")
    md.append("### Recommendation\n")
    md.append(f"Target **risk-adjusted** performance, not a daily percentage. The "
              f"highest-Sharpe, walk-forward-validated configuration here is "
              f"**{best_key}** under the managed risk policy (ATR position sizing, "
              f"2% risk/trade, 2.5-ATR stops, 5% daily-loss halt, 25% max-drawdown "
              f"kill switch). Realistic, sustainable goals for a single-asset "
              f"system like this are low-single-digit **monthly** returns with a "
              f"Sharpe above 1 and drawdowns held under ~20%. Push returns higher "
              f"only via diversification (many uncorrelated symbols/strategies), "
              f"not leverage on one bet.\n")
    print("\n".join(md))
    return "\n".join(md)


# --------------------------------------------------------------------------
def main():
    # Windows consoles default to cp1252, which cannot encode chars like "approx".
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true", help="smaller windows for a quick run")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    strategies = list(STRATEGY_REGISTRY.keys())
    train, test = (1500, 700) if args.fast else (2160, 720)

    header = (
        "# Strategy Evaluation & 10%/day Reality Check\n\n"
        f"- Costs: commission {COMMISSION_PCT*100:.2f}%/side, slippage {SLIPPAGE_BPS} bps\n"
        f"- Capital: ${INITIAL_CAPITAL:,.0f}; bars: hourly; data: real BTCUSDT 1h + 4 synthetic regimes\n"
        f"- Risk policy: ATR sizing (2% risk/trade, 2.5-ATR stop), max 50% position, "
        f"5% daily-loss halt, 25% max-drawdown kill switch\n"
        f"- Walk-forward: train {train} bars / test {test} bars, re-optimised by Sharpe each window\n"
    )

    part1 = regime_stress_test(strategies)
    wf_results, part2 = walk_forward_eval(strategies, train, test)

    # Best by OOS Sharpe (risk-adjusted), among strategies that actually traded.
    candidates = {k: w for k, w in wf_results.items()
                  if w.oos_report.n_trades > 0 and k != "buyhold"}
    if not candidates:
        candidates = wf_results
    best_key = max(candidates, key=lambda k: candidates[k].oos_report.sharpe)
    part3 = verdict(best_key, wf_results[best_key])

    # Standalone OOS dashboard for the winner.
    best_wf = wf_results[best_key]
    dash = os.path.join(OUT_DIR, f"oos_{best_key}.html")
    if not best_wf.oos_equity_df.empty:
        generate_html_report(
            best_wf.oos_equity_df, best_wf.oos_trade_log, best_wf.oos_report, dash,
            title=f"OUT-OF-SAMPLE {best_key.upper()} | BTCUSDT 1h | walk-forward",
        )
        print(f"\nOOS dashboard -> {dash}")

    report_md = "\n".join([header, part1, part2, part3])
    md_path = os.path.join(OUT_DIR, "strategy_evaluation.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report_md)
    print(f"Written report -> {md_path}")


if __name__ == "__main__":
    main()
