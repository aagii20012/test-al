"""Self-contained HTML performance dashboard.

Produces a single standalone .html file with no external dependencies:
charts are rendered as inline SVG built in pure Python, so the report opens
offline in any browser and can be emailed or committed as an artifact.

Sections:
  * headline metric cards,
  * equity curve,
  * underwater (drawdown) chart,
  * recent closed-trade table.
"""

from __future__ import annotations

import html
from typing import List, Sequence

import pandas as pd

from .performance import PerformanceReport


# --------------------------------------------------------------------------
# Tiny SVG charting helpers (no JS, no libraries)
# --------------------------------------------------------------------------
def _scale(values: Sequence[float], lo: float, hi: float, out_lo: float, out_hi: float) -> List[float]:
    span = (hi - lo) or 1.0
    return [out_lo + (v - lo) / span * (out_hi - out_lo) for v in values]


def _line_chart(
    series: Sequence[float],
    width: int = 920,
    height: int = 260,
    pad: int = 40,
    stroke: str = "#4f8cff",
    fill: str = "rgba(79,140,255,0.12)",
    baseline: float | None = None,
) -> str:
    if len(series) < 2:
        return '<svg></svg>'

    lo, hi = min(series), max(series)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)

    n = len(series)
    xs = _scale(range(n), 0, n - 1, pad, width - pad)
    ys = _scale(series, lo, hi, height - pad, pad)  # invert: high value -> small y

    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{pad},{height - pad} {pts} {width - pad},{height - pad}"

    # horizontal gridlines + y labels
    grid = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = lo + (hi - lo) * frac
        y = height - pad - frac * (height - 2 * pad)
        grid.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" '
                    f'stroke="#222a3a" stroke-width="1"/>')
        grid.append(f'<text x="{pad - 6}" y="{y + 4:.1f}" text-anchor="end" '
                    f'class="axis">{val:,.0f}</text>')
    grid_svg = "\n".join(grid)

    return f"""<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img">
  {grid_svg}
  <polygon points="{area}" fill="{fill}" stroke="none"/>
  <polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="2"/>
</svg>"""


def _drawdown_series(equity: pd.Series) -> List[float]:
    running_max = equity.cummax()
    return ((equity - running_max) / running_max * 100).tolist()


# --------------------------------------------------------------------------
def _metric_cards(report: PerformanceReport) -> str:
    def card(label, value, good=None):
        cls = "" if good is None else ("pos" if good else "neg")
        return (f'<div class="card"><div class="card-label">{label}</div>'
                f'<div class="card-value {cls}">{value}</div></div>')

    cards = [
        card("Total return", f"{report.total_return_pct:,.2f}%", report.total_return_pct >= 0),
        card("CAGR", f"{report.cagr_pct:,.2f}%", report.cagr_pct >= 0),
        card("Sharpe", f"{report.sharpe:,.2f}", report.sharpe >= 1),
        card("Sortino", f"{report.sortino:,.2f}", report.sortino >= 1),
        card("Max drawdown", f"{report.max_drawdown_pct:,.2f}%", report.max_drawdown_pct > -20),
        card("Calmar", f"{report.calmar:,.2f}", report.calmar >= 1),
        card("Volatility (ann.)", f"{report.annual_volatility_pct:,.2f}%"),
        card("VaR 95%", f"{report.var_95_pct:,.2f}%"),
        card("Win rate", f"{report.win_rate_pct:,.1f}%", report.win_rate_pct >= 50),
        card("Closed trades", f"{report.n_trades}"),
        card("Final equity", f"{report.final_equity:,.0f}"),
        card("Commission paid", f"{report.total_commission:,.0f}"),
    ]
    return "\n".join(cards)


def _trade_rows(trade_log: list, limit: int = 50) -> str:
    if not trade_log:
        return '<tr><td colspan="5" class="muted">No closed trades.</td></tr>'
    rows = []
    for t in trade_log[-limit:][::-1]:
        pnl = t.get("realized_pnl", 0.0)
        cls = "pos" if pnl >= 0 else "neg"
        dt = html.escape(str(t.get("dt", "")))
        rows.append(
            f"<tr><td>{dt}</td><td>{html.escape(str(t['symbol']))}</td>"
            f"<td>{html.escape(str(t['side']))}</td>"
            f"<td>{t['qty']:.6f}</td>"
            f"<td class='{cls}'>{pnl:,.2f}</td></tr>"
        )
    return "\n".join(rows)


def generate_html_report(
    equity_df: pd.DataFrame,
    trade_log: list,
    report: PerformanceReport,
    path: str,
    title: str = "Backtest Report",
) -> str:
    equity = equity_df["equity"] if "equity" in equity_df else pd.Series(dtype=float)
    equity_svg = _line_chart(equity.tolist(), baseline=report.initial_equity)
    dd_svg = _line_chart(_drawdown_series(equity) if len(equity) else [],
                         stroke="#ff5f6d", fill="rgba(255,95,109,0.14)", baseline=0.0)

    start = html.escape(str(equity_df.index[0])) if len(equity_df) else "-"
    end = html.escape(str(equity_df.index[-1])) if len(equity_df) else "-"

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0d1117; color:#e6edf3;
         font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b949e; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:10px; padding:14px 16px; }}
  .card-label {{ color:#8b949e; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card-value {{ font-size:22px; font-weight:600; margin-top:6px; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }}
  section {{ margin-top:34px; }}
  h2 {{ font-size:15px; color:#c9d1d9; border-bottom:1px solid #21262d; padding-bottom:8px; }}
  .chartbox {{ background:#161b22; border:1px solid #21262d; border-radius:10px;
              padding:12px; overflow-x:auto; }}
  svg {{ width:100%; height:auto; display:block; }}
  .axis {{ fill:#6e7681; font-size:10px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th,td {{ text-align:right; padding:8px 10px; border-bottom:1px solid #21262d; }}
  th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),
  th:nth-child(3),td:nth-child(3) {{ text-align:left; }}
  th {{ color:#8b949e; font-weight:500; font-size:12px; }}
  .muted {{ color:#6e7681; text-align:center; }}
  footer {{ margin-top:40px; color:#6e7681; font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{html.escape(title)}</h1>
  <div class="sub">{start} &rarr; {end} &middot; {len(equity_df)} bars</div>

  <div class="grid">{_metric_cards(report)}</div>

  <section>
    <h2>Equity curve</h2>
    <div class="chartbox">{equity_svg}</div>
  </section>

  <section>
    <h2>Drawdown (%)</h2>
    <div class="chartbox">{dd_svg}</div>
  </section>

  <section>
    <h2>Recent closed trades</h2>
    <div class="chartbox">
      <table>
        <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Realized P&amp;L</th></tr></thead>
        <tbody>{_trade_rows(trade_log)}</tbody>
      </table>
    </div>
  </section>

  <footer>Generated by algotrading &middot; charts are inline SVG, no external assets.</footer>
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path
