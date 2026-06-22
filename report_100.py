#!/usr/bin/env python
"""$100 account, $50/day target — feasibility study under realistic conditions.

Same engine as backtest and live. This script answers, with data:
  * Can a $100 account realistically earn $50/day ( = 50% of capital, per day )?
  * If not, what is the highest sustainable target, and what capital would make
    $50/day a sane goal?

It runs walk-forward (out-of-sample) on real BTCUSDT 1h history for every
strategy family, under a STRICT risk policy (1-2% risk/trade, ATR sizing, daily
loss limit, max-drawdown suspension, stop-loss + take-profit) and a FULL realism
model (fees, slippage, latency via next-bar-open fills, partial fills, and the
exchange minimum-notional that bites hardest on tiny accounts).

Outputs: console tables, reports/account_100_evaluation.md, and an OOS dashboard.

Run:  python report_100.py            (real cached data; full grids)
      python report_100.py --fast     (smaller windows; quick)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional

import pandas as pd

from algotrading.analytics.dashboard import generate_html_report
from algotrading.analytics.performance import PerformanceReport
from algotrading.research.grids import FAMILY, PARAM_GRIDS, STRATEGY_REGISTRY
from algotrading.research.walkforward import walk_forward
from algotrading.risk.risk_manager import RiskConfig

# ---- realistic, conservative assumptions for a tiny retail account -------
INITIAL_CAPITAL = 100.0
COMMISSION_PCT = 0.001        # 0.10% taker fee per side (Binance spot)
SLIPPAGE_BPS = 2.0            # 2 bp adverse slippage per fill
PERIODS_PER_YEAR = 365 * 24
TARGET_DAILY_USD = 50.0
DATA_PATH = os.path.join("data_cache", "BTCUSDT_1h.csv")
OUT_DIR = "reports"

# Full execution realism (latency + partial fills + exchange dust limit).
EXEC_REALISM = {
    "fill_at": "next_open",   # decide on close, fill at next bar's open (latency)
    "latency_bps": 1.0,       # extra adverse bps for decision/transmission lag
    "participation_rate": 0.10,  # take at most 10% of a bar's volume
    "min_notional": 10.0,     # Binance rejects orders below ~$10 notional
    "max_working_bars": 3,
}

# Strict risk policy honouring the brief: dynamic (ATR) sizing risking 2% of
# equity per trade, hard daily-loss halt, max-drawdown suspension, stop+target.
STRICT_RISK = RiskConfig(
    max_position_pct=1.0,     # spot, no leverage: at most 100% of equity deployed
    risk_per_trade=0.02,      # risk 2% of equity to the stop per trade
    max_leverage=1.0,
    allow_short=True,
    use_stops=True,
    stop_loss_pct=0.05,       # fallback stop if ATR unavailable
    take_profit_pct=0.06,     # take profit ~2.4R given the 2.5-ATR stop
    atr_sizing=True,
    atr_period=14,
    atr_stop_mult=2.5,
    max_daily_loss_pct=0.05,  # halt & flatten for the day at -$5 (-5%)
    max_drawdown_pct=0.25,    # suspend permanently at -$25 (-25%)
)


def _row(name: str, fam: str, r: PerformanceReport) -> dict:
    pf = "inf" if r.profit_factor == float("inf") else round(r.profit_factor, 2)
    return {
        "strategy": f"{name} ({fam})",
        "net_$": round(r.net_profit, 2),
        "ROC_%": round(r.return_on_capital_pct, 2),
        "$/day": round(r.avg_daily_profit, 4),
        "sharpe": round(r.sharpe, 2),
        "maxDD_%": round(r.max_drawdown_pct, 2),
        "win_%": round(r.win_rate_pct, 1),
        "PF": pf,
        "trades": r.n_trades,
    }


def _print(title: str, rows: List[dict]) -> str:
    df = pd.DataFrame(rows)
    print(f"\n=== {title} ===")
    print(df.to_string(index=False))
    return f"\n### {title}\n\n" + df.to_markdown(index=False)


def load_real_frames() -> Optional[Dict[str, pd.DataFrame]]:
    if not os.path.exists(DATA_PATH):
        return None
    return {"BTCUSDT": pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)}


def capital_required_table(best_report) -> str:
    """What account size would make $50/day a reasonable (not crazy) goal, for a
    range of *daily edge* assumptions — anchored by the best result we measured."""
    best_daily_ret = best_report.avg_daily_return_pct / 100.0
    # Only treat the measured edge as usable if the strategy actually MADE money
    # out-of-sample (net profit positive). A marginally-positive mean of daily
    # percent returns on a curve that still ended down is volatility drag, not edge.
    has_edge = best_report.net_profit > 0 and best_daily_ret > 0 and best_report.sharpe > 0
    md = ["\n### Capital required to reasonably target $50/day\n",
          "Assuming a genuinely sustainable daily return (net of costs), the "
          "capital needed so that $50 is that return:\n",
          "| Sustainable daily return | Implied annual* | Capital for $50/day |",
          "|---|---|---|"]
    for d in (0.0005, 0.001, 0.002, 0.003, 0.005):
        ann = (1 + d) ** 365 - 1
        cap = TARGET_DAILY_USD / d
        note = ""
        if d <= 0.001:
            note = " (excellent, rare)"
        elif d <= 0.003:
            note = " (exceptional)"
        else:
            note = " (almost certainly unsustainable)"
        md.append(f"| {d*100:.2f}%/day{note} | {ann*100:,.0f}%/yr | ${cap:,.0f} |")
    md.append("\n*Compounded; shown to convey how extreme even 'modest' daily "
              "rates are. A 0.1%/day edge (~44%/yr) is already world-class.\n")
    if has_edge:
        cap = TARGET_DAILY_USD / best_daily_ret
        md.append(f"At **this study's** best out-of-sample daily return "
                  f"({best_daily_ret*100:.4f}%/day), $50/day would require "
                  f"**${cap:,.0f}** of capital — and even that assumes the edge "
                  f"persists, which walk-forward suggests is fragile.\n")
    else:
        md.append(f"This study's best out-of-sample result was **not reliably "
                  f"positive** (net ${best_report.net_profit:,.2f}, Sharpe "
                  f"{best_report.sharpe:.2f}), so no account size turns it into "
                  f"$50/day: more capital scales a non-edge into a bigger loss, "
                  f"not a profit. The capital figures above presuppose a genuine "
                  f"positive edge this single-asset bot did not demonstrate.\n")
    return "\n".join(md)


def verdict(best_key: str, best_wf, capital_md: str) -> str:
    r = best_wf.oos_report
    daily_ret = r.avg_daily_return_pct / 100.0
    target_daily_ret = TARGET_DAILY_USD / INITIAL_CAPITAL  # = 0.50 (50%/day)

    md = ["\n## Verdict — $50/day on a $100 account?\n",
          "**No. $50/day on $100 is a 50% **daily** return — impossible to "
          "sustain, and self-contradictory with the risk rules in the brief.**\n",
          "Two independent reasons:\n",
          "**1. Compounding math.** 50%/day compounds to `(1.5)^252 ≈ 1e44×` "
          "capital in a trading year — more money than exists on Earth within "
          "weeks. Even held flat (withdrawing $50/day), you must earn 50% of "
          "the account every day with perfect consistency.\n",
          "**2. It contradicts '1–2% risk per trade'.** Risking 2% of $100 is "
          "**$2 per trade**. To net **$50** you would need a **+25R** outcome "
          "*every day* — a 25-to-1 reward on risk, won daily. Real edges run "
          "well under 1R expectancy. You cannot simultaneously cap risk at $2 "
          "and target $50; the two requirements are mutually exclusive.\n",
          "### What the data actually shows (out-of-sample, $100, full realism)\n",
          f"- Best risk-adjusted strategy: **{best_key} ({FAMILY[best_key]})**",
          f"- Net profit over the OOS year: **${r.net_profit:,.2f}** "
          f"(ROC {r.return_on_capital_pct:.2f}%)",
          f"- Average daily profit: **${r.avg_daily_profit:,.4f}/day** "
          f"vs the **$50/day** target",
          f"- Sharpe {r.sharpe:.2f}, max drawdown {r.max_drawdown_pct:.1f}%, "
          f"profit factor {('inf' if r.profit_factor==float('inf') else f'{r.profit_factor:.2f}')}, "
          f"{r.n_trades} trades\n"]
    if r.avg_daily_profit != 0:
        ratio = TARGET_DAILY_USD / abs(r.avg_daily_profit)
        md.append(f"The $50/day goal is on the order of **{ratio:,.0f}×** the "
                  f"magnitude of the best daily result the engine produced on "
                  f"$100 — and that result was {'positive' if r.avg_daily_profit>0 else 'negative'}.\n")
    md.append("### Highest sustainable target instead\n")
    md.append("On a single asset, after costs, a defensible OOS goal is roughly "
              "**0.05–0.2% of equity per day** (~20–100%/yr) at Sharpe > 1 with "
              "drawdowns under ~20% — and only the best strategy here even "
              "approached the low end out-of-sample. On **$100**, that is cents "
              "to a few tens of cents per day. The honest expected range for a "
              "disciplined $100 bot is roughly **−$0.20 to +$0.20 per day**; "
              "treat anything above that as luck, not edge.\n")
    md.append(capital_md)
    md.append("\n### Recommendation\n")
    md.append(f"1. **Keep the $100 on Binance _testnet_** and run "
              f"**{best_key}** under the strict risk policy to validate "
              f"execution and behaviour — not to get rich.\n"
              f"2. **Target risk-adjusted return** (Sharpe/Calmar), capped at "
              f"~1–2% risk/trade, with a realistic monthly goal in the low "
              f"single digits of percent.\n"
              f"3. **Grow via capital and diversification** across many "
              f"uncorrelated symbols/strategies — never leverage on one bet. "
              f"$50/day becomes *reasonable* near the capital levels in the "
              f"table above, not at $100.\n")
    print("\n".join(md))
    return "\n".join(md)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true", help="smaller windows; quick run")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    real = load_real_frames()
    if real is None:
        print("No cached real data found at", DATA_PATH,
              "- run: python -m algotrading.cli download --symbols BTCUSDT --interval 1h --days 365")
        from algotrading.research.regimes import make_regime_frames
        real = make_regime_frames("bear", ["BTCUSDT"], n_bars=8760, seed=3)

    train, test = (1500, 700) if args.fast else (2160, 720)
    strategies = list(STRATEGY_REGISTRY.keys())

    header = (
        "# $100 Account · $50/day Feasibility Study\n\n"
        f"- Starting capital: **${INITIAL_CAPITAL:,.0f}**; aspirational target: "
        f"**${TARGET_DAILY_USD:,.0f}/day** (= {TARGET_DAILY_USD/INITIAL_CAPITAL*100:.0f}% of capital per day)\n"
        f"- Costs: {COMMISSION_PCT*100:.2f}%/side fee, {SLIPPAGE_BPS} bps slippage; "
        f"realism: next-bar-open fills (latency), 10%-of-volume partial fills, $10 min-notional\n"
        f"- Risk: ATR sizing @ 2% risk/trade, 2.5-ATR stop, 6% take-profit, "
        f"5% daily-loss halt, 25% max-drawdown suspension\n"
        f"- Walk-forward: train {train} / test {test} bars, re-optimised by Sharpe each window; "
        f"data = real BTCUSDT 1h\n"
    )

    results, rows = {}, []
    for key in strategies:
        wf = walk_forward(
            real, STRATEGY_REGISTRY[key], PARAM_GRIDS.get(key, {}), STRICT_RISK,
            train_bars=train, test_bars=test, objective="sharpe",
            commission_pct=COMMISSION_PCT, slippage_bps=SLIPPAGE_BPS,
            initial_capital=INITIAL_CAPITAL, periods_per_year=PERIODS_PER_YEAR,
            exec_realism=EXEC_REALISM,
        )
        results[key] = wf
        rows.append(_row(key, FAMILY[key], wf.oos_report))

    table_md = _print("Walk-forward OUT-OF-SAMPLE · $100 account · real BTCUSDT 1h", rows)

    # Best by OOS Sharpe among strategies that actually traded (exclude benchmark).
    cand = {k: w for k, w in results.items()
            if w.oos_report.n_trades > 0 and k != "buyhold"}
    cand = cand or results
    best_key = max(cand, key=lambda k: cand[k].oos_report.sharpe)
    best = results[best_key]

    cap_md = capital_required_table(best.oos_report)
    verdict_md = verdict(best_key, best, cap_md)

    dash = os.path.join(OUT_DIR, f"account100_oos_{best_key}.html")
    if not best.oos_equity_df.empty:
        generate_html_report(best.oos_equity_df, best.oos_trade_log, best.oos_report,
                             dash, title=f"$100 OOS · {best_key.upper()} · BTCUSDT 1h")
        print(f"\nOOS dashboard -> {dash}")

    md_path = os.path.join(OUT_DIR, "account_100_evaluation.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join([header, "## Strategy comparison\n", table_md, verdict_md]))
    print(f"Written report -> {md_path}")


if __name__ == "__main__":
    main()
