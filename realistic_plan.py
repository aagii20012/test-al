#!/usr/bin/env python
"""Realistic staged plan on a real $100 account — show the compounding curve.

Capital-preservation first: risk 1%/trade, cap the daily loss at 1%, bank the
day's win at +1%, hard kill switch at 15% drawdown. Full realistic costs
(fees, slippage, latency, partial fills, $10 min-notional, 10% APR financing).
Walk-forward (out-of-sample) on real BTCUSDT 1h, capital compounded forward.

Reports gross vs net, daily up/down/banked counts, and writes an equity-curve
dashboard so the actual $100 path is visible.

Run:  python realistic_plan.py            (real cached data)
      python realistic_plan.py --fast
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import pandas as pd

from algotrading.analytics.dashboard import generate_html_report
from algotrading.research.grids import FAMILY, PARAM_GRIDS, STRATEGY_REGISTRY
from algotrading.research.walkforward import walk_forward
from algotrading.risk.risk_manager import RiskConfig

INITIAL_CAPITAL = 100.0
PERIODS_PER_YEAR = 365 * 24
DATA_PATH = os.path.join("data_cache", "BTCUSDT_1h.csv")
OUT_DIR = "reports"

# Mirrors config/config.yaml (the realistic staged plan).
REALISTIC_RISK = RiskConfig(
    atr_sizing=True, risk_per_trade=0.01, atr_stop_mult=2.5,
    max_position_pct=0.50, max_leverage=1.0, stop_loss_pct=0.05,
    use_stops=True, cash_buffer=0.003, allow_short=True,
    max_daily_loss_pct=0.01, max_daily_profit_pct=0.01, max_drawdown_pct=0.15,
)
EXEC = {"fill_at": "next_open", "latency_bps": 1.0, "participation_rate": 0.10,
        "min_notional": 10.0, "impact_coeff_bps": 50.0}
FINANCING_APR = 0.10
STRATS = ["momentum", "volatility", "rsi", "donchian", "buyhold"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    if not os.path.exists(DATA_PATH):
        print("No cached data at", DATA_PATH)
        return
    frames = {"BTCUSDT": pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)}
    train, test = (1500, 700) if args.fast else (2160, 720)

    rows, results = [], {}
    for key in STRATS:
        wf = walk_forward(
            frames, STRATEGY_REGISTRY[key], PARAM_GRIDS.get(key, {}), REALISTIC_RISK,
            train_bars=train, test_bars=test, objective="sharpe",
            commission_pct=0.001, slippage_bps=2.0, initial_capital=INITIAL_CAPITAL,
            periods_per_year=PERIODS_PER_YEAR, exec_realism=EXEC, financing_apr=FINANCING_APR,
        )
        results[key] = wf
        r = wf.oos_report
        eq = wf.oos_equity_df["equity"].resample("1D").last().dropna() if not wf.oos_equity_df.empty else pd.Series(dtype=float)
        daily = eq.diff().dropna()
        up = int((daily > 0.005).sum()); down = int((daily < -0.005).sum())
        rows.append({
            "strategy": f"{key} ({FAMILY[key]})",
            "start_$": INITIAL_CAPITAL,
            "end_$": round(r.final_equity, 2),
            "gross_%": round(r.gross_return_pct, 1),
            "net_%": round(r.total_return_pct, 1),
            "maxDD_%": round(r.max_drawdown_pct, 1),
            "up_days": up, "down_days": down,
            "best_day_$": round(daily.max(), 2) if len(daily) else 0.0,
            "worst_day_$": round(daily.min(), 2) if len(daily) else 0.0,
            "sharpe": round(r.sharpe, 2),
        })

    df = pd.DataFrame(rows)
    print("\n=== REALISTIC staged plan — walk-forward OOS, real $100 BTCUSDT 1h ===")
    print(df.to_string(index=False))

    # Best by capital preserved (highest ending balance).
    best = max(results, key=lambda k: results[k].oos_report.final_equity)
    bw = results[best]
    dash = os.path.join(OUT_DIR, f"realistic_{best}.html")
    if not bw.oos_equity_df.empty:
        generate_html_report(bw.oos_equity_df, bw.oos_trade_log, bw.oos_report, dash,
                             title=f"REALISTIC plan · {best.upper()} · $100 · BTCUSDT 1h OOS")
        print(f"\nEquity-curve dashboard -> {dash}")

    with open(os.path.join(OUT_DIR, "realistic_plan.md"), "w", encoding="utf-8") as fh:
        fh.write("# Realistic staged plan on a $100 account\n\n"
                 "- Risk 1%/trade, daily loss cap 1%, daily win-bank 1%, 15% drawdown kill switch\n"
                 "- Full costs: fees, slippage, latency, partial fills, $10 min-notional, 10% APR financing\n"
                 "- Walk-forward OOS, capital compounded forward, real BTCUSDT 1h\n\n"
                 + df.to_markdown(index=False) + "\n")
    print(f"Written -> {os.path.join(OUT_DIR, 'realistic_plan.md')}")
    print(f"\nBest at preserving capital: {best} -> ${bw.oos_report.final_equity:.2f} "
          f"from ${INITIAL_CAPITAL:.0f}")


if __name__ == "__main__":
    main()
