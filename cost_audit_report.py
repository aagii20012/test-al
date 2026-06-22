#!/usr/bin/env python
"""Gross-vs-net cost audit: rerun every strategy with and without trading costs.

For each strategy we run the IDENTICAL walk-forward twice, changing ONLY the
monetary costs (everything else — fill timing, participation, min-notional,
risk policy, data, parameters searched — is held fixed), so the gap between the
two curves is exactly the trading-cost drag:

  * GROSS  — commission = 0, slippage = 0, latency penalty = 0, financing = 0,
             market impact = 0  (the strategy's raw edge)
  * NET    — realistic Binance-spot costs: 0.10%/side fee, 2 bps slippage,
             next-bar-open fills + 1 bp latency, 10%-of-volume partial fills,
             $10 min-notional, market impact, and 10% APR short-borrow/financing

Outputs the gross return, net return, the cost drag, and the dollar breakdown
(commission / slippage / financing) per strategy, plus reports/cost_audit.md.

Run:  python cost_audit_report.py            (real cached data)
      python cost_audit_report.py --fast     (smaller windows)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Optional

import pandas as pd

from algotrading.research.grids import FAMILY, PARAM_GRIDS, STRATEGY_REGISTRY
from algotrading.research.walkforward import walk_forward
from algotrading.risk.risk_manager import RiskConfig

INITIAL_CAPITAL = 100.0
PERIODS_PER_YEAR = 365 * 24
DATA_PATH = os.path.join("data_cache", "BTCUSDT_1h.csv")
OUT_DIR = "reports"

# Risk policy is identical for gross and net (it only sizes/limits trades).
STRICT_RISK = RiskConfig(
    max_position_pct=1.0, risk_per_trade=0.02, max_leverage=1.0, allow_short=True,
    use_stops=True, stop_loss_pct=0.05, take_profit_pct=0.06,
    atr_sizing=True, atr_period=14, atr_stop_mult=2.5,
    max_daily_loss_pct=0.05, max_drawdown_pct=0.25, cash_buffer=0.003,
)

# Execution microstructure held FIXED across gross/net (so the same trades fire);
# only the monetary penalties differ.
BASE_EXEC = {"fill_at": "next_open", "participation_rate": 0.10,
             "min_notional": 10.0, "max_working_bars": 3}

# Realistic NET costs.
NET = dict(commission_pct=0.001, slippage_bps=2.0, financing_apr=0.10,
           exec_realism={**BASE_EXEC, "latency_bps": 1.0, "impact_coeff_bps": 50.0})
# GROSS: zero every monetary cost, keep the microstructure identical.
GROSS = dict(commission_pct=0.0, slippage_bps=0.0, financing_apr=0.0,
             exec_realism={**BASE_EXEC, "latency_bps": 0.0, "impact_coeff_bps": 0.0})


def load_real() -> Optional[Dict[str, pd.DataFrame]]:
    if not os.path.exists(DATA_PATH):
        return None
    return {"BTCUSDT": pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    real = load_real()
    if real is None:
        from algotrading.research.regimes import make_regime_frames
        real = make_regime_frames("bear", ["BTCUSDT"], n_bars=8760, seed=3)
    train, test = (1500, 700) if args.fast else (2160, 720)

    def wf(strategy_cls, grid, costs):
        return walk_forward(
            real, strategy_cls, grid, STRICT_RISK,
            train_bars=train, test_bars=test, objective="sharpe",
            initial_capital=INITIAL_CAPITAL, periods_per_year=PERIODS_PER_YEAR, **costs,
        )

    rows = []
    for key in STRATEGY_REGISTRY:
        g = wf(STRATEGY_REGISTRY[key], PARAM_GRIDS.get(key, {}), GROSS)
        n = wf(STRATEGY_REGISTRY[key], PARAM_GRIDS.get(key, {}), NET)
        gr, nr = g.oos_report, n.oos_report
        rows.append({
            "strategy": f"{key} ({FAMILY[key]})",
            "gross_ret_%": round(gr.total_return_pct, 2),
            "net_ret_%": round(nr.total_return_pct, 2),
            "cost_drag_%": round(gr.total_return_pct - nr.total_return_pct, 2),
            "commission_$": round(nr.total_commission, 2),
            "slippage_$": round(nr.total_slippage, 2),
            "financing_$": round(nr.total_financing, 2),
            "total_cost_$": round(nr.total_costs, 2),
            "net_sharpe": round(nr.sharpe, 2),
        })

    df = pd.DataFrame(rows)
    print("\n=== GROSS vs NET — walk-forward OOS, $100, real BTCUSDT 1h ===")
    print(df.to_string(index=False))

    header = (
        "# Gross-vs-Net Cost Audit\n\n"
        f"- Capital ${INITIAL_CAPITAL:,.0f}; walk-forward train {train}/test {test} bars; real BTCUSDT 1h\n"
        "- GROSS = all monetary costs zeroed; NET = 0.10%/side fee, 2 bps slippage, "
        "next-open fills +1 bp latency, 10%-volume partial fills, $10 min-notional, "
        "market impact, 10% APR short-borrow/financing\n"
        "- Microstructure (fill timing, participation, min-notional, risk policy, params) "
        "is identical across both, so the gap is purely trading cost.\n\n"
        "## Where each cost is applied\n"
        "| Cost | Rate set | Applied (equation) | Hits cash | Reported |\n"
        "|---|---|---|---|---|\n"
        "| Commission | `simulated.py` ctor / CLI `--commission` | `qty*fill_price*commission_pct` (simulated.py) | `portfolio.update_fill`: `cash -= commission` | `total_commission` |\n"
        "| Slippage+latency | `slippage_bps`,`latency_bps` | `fill_price = ref*(1±adverse)` (simulated.py) | embedded in `fill_price` debit | `total_slippage` (new) |\n"
        "| Market impact | `impact_coeff_bps` | `adverse += impact*participation` (simulated.py) | embedded in `fill_price` | in `total_slippage` |\n"
        "| Financing/borrow | `financing_apr` | `base*apr/periods_per_year` per bar (portfolio._accrue_financing) | `cash -= charge` each bar | `total_financing` (new) |\n"
        "| Live fees | Binance response | `sum(fills[].commission)` (binance.py) | real exchange | `total_commission` |\n\n"
        "## Gross vs net by strategy\n\n"
    )
    with open(os.path.join(OUT_DIR, "cost_audit.md"), "w", encoding="utf-8") as fh:
        fh.write(header + df.to_markdown(index=False) + "\n")
    print(f"\nWritten -> {os.path.join(OUT_DIR, 'cost_audit.md')}")


if __name__ == "__main__":
    main()
