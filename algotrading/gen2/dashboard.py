"""Generation-2 scoreboard data + a static HTML preview.

The dashboard is *sourced exclusively* from a single Generation-2 experiment
directory (``state/gen2/<experiment_id>/``). It reads the immutable manifest and,
if the experiment has published, resolves the single ``CURRENT`` pointer to its
fully hash-verified checkpoint — and nothing else. It NEVER opens a Generation-1
file (``state/*_sim.json``); the only thing it can say about Generation 1 is the
fixed "INVALIDATED" notice.

Reading rules that make the board trustworthy:
  * The ONLY published state is the checkpoint named by ``CURRENT``. The dashboard
    resolves it through :func:`checkpoint.resolve_current`, which verifies the
    checkpoint-manifest hash + every artifact hash before returning anything. It
    never lists the ``checkpoints/`` directory or picks a "latest" file, so an
    orphan checkpoint (materialised but not yet pointed-to, or left by a crash) is
    invisible here — exactly as it is to the coordinator.
  * A corrupt / mismatched ``CURRENT`` or checkpoint raises (fail-closed) rather
    than rendering a stale or half-written board.
  * A PREPARED experiment renders a scoreboard that is explicitly "not trading
    yet" — the 8 bots are listed at their funding, with no live results.
  * Only an ACTIVE experiment whose CURRENT checkpoint is a live (non-dry-run)
    tick shows live per-bot equity / return / position. A dry-run checkpoint is a
    rehearsal and never surfaces as results.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from . import checkpoint as cp

GEN1_NOTICE = (
    "Generation 1 is INVALIDATED (position-desync + reversal cost-basis defects). "
    "Its results are frozen forensic evidence and are NOT shown here."
)


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_scoreboard(exp_dir: str) -> dict:
    """Assemble the scoreboard payload from ONE experiment directory only.

    Reads ``manifest.json`` (required) and, if the experiment has published,
    resolves ``CURRENT`` -> its verified checkpoint for live per-bot results.
    Reads nothing outside ``exp_dir`` and nothing outside the published
    checkpoint.
    """
    manifest = _read_json(os.path.join(exp_dir, "manifest.json"))
    if manifest is None:
        raise FileNotFoundError(
            f"No Generation-2 manifest under {exp_dir!r}; nothing to display.")

    # The single published checkpoint (or None if never published). This verifies
    # the CURRENT pointer + every artifact hash; a broken store fails closed here.
    published = cp.resolve_current(
        exp_dir, experiment_id=manifest["experiment_id"])

    # A dry-run checkpoint is a rehearsal, not live trading: it MUST NOT surface as
    # results. Live per-bot numbers come only from a real (non-dry-run) published
    # checkpoint while the experiment is ACTIVE.
    live = published if (published is not None and not published.dry_run) else None
    run_status = live.run_status if live is not None else {}

    results = {}
    for row in run_status.get("bots", []):
        results[row["bot_id"]] = row

    status = manifest["status"]
    trading = status == "ACTIVE" and live is not None
    capital = float(manifest["capital_per_bot"])

    bots = []
    for b in manifest["bots"]:
        row = {
            "bot_id": b["bot_id"],
            "strategy": b["strategy"],
            "symbol": b["symbol"],
            "initial_capital": capital,
        }
        r = results.get(b["bot_id"])
        if r is not None:
            equity = float(r.get("equity", capital))
            row.update({
                "equity": equity,
                "cash": float(r.get("cash", capital)),
                "position": float(r.get("position", 0.0)),
                "realized_pnl": float(r.get("realized_pnl", 0.0)),
                "last_price": float(r.get("last_price", 0.0)),
                "last_bar_ts": int(r.get("last_bar_ts", 0)),
                "return_pct": (equity / capital - 1.0) * 100.0 if capital else 0.0,
                "has_results": True,
            })
        else:
            row.update({
                "equity": capital, "cash": capital, "position": 0.0,
                "realized_pnl": 0.0, "last_price": 0.0, "last_bar_ts": 0,
                "return_pct": 0.0, "has_results": False,
            })
        bots.append(row)

    # Rank leaders first only when we actually have results.
    if trading:
        bots.sort(key=lambda x: x["equity"], reverse=True)

    return {
        "generation": manifest["generation"],
        "schema_version": manifest["schema_version"],
        "experiment_id": manifest["experiment_id"],
        "status": status,
        "trading": trading,
        "created_utc": manifest["created_utc"],
        "capital_per_bot": capital,
        "code": manifest.get("code", {}),
        "activation_commit": manifest.get("activation_commit"),
        "cost_model": manifest.get("cost_model", {}),
        "market": manifest.get("market", {}),
        "checkpoint": (live.name if live is not None else None),
        "checkpoint_manifest_sha256": (
            live.checkpoint_manifest_sha256 if live is not None else None),
        "as_of": run_status.get("published_utc"),
        "decision_epoch_ms": run_status.get("decision_epoch_ms"),
        "snapshot_sha256": run_status.get("snapshot_sha256"),
        "dry_run": bool(live.dry_run) if live is not None else False,
        "bots": bots,
        "gen1_notice": GEN1_NOTICE,
    }


def render_html(scoreboard: dict) -> str:
    """Self-contained, offline HTML preview of the Gen2 scoreboard."""
    status = scoreboard["status"]
    trading = scoreboard["trading"]
    badge = {
        "PREPARED": ("#b58900", "PREPARED — built &amp; bound, NOT trading yet"),
        "ACTIVE": ("#2aa198", "ACTIVE — live paper trading"),
        "PAUSED": ("#cb4b16", "PAUSED"),
        "FAILED": ("#dc322f", "FAILED — a tick aborted; investigate"),
        "CLOSED": ("#586e75", "CLOSED"),
    }.get(status, ("#586e75", status))

    rows = []
    for i, b in enumerate(scoreboard["bots"], 1):
        if b["has_results"]:
            ret = b["return_pct"]
            colour = "#2aa198" if ret >= 0 else "#dc322f"
            equity = f"${b['equity']:,.2f}"
            retc = f"<span style='color:{colour}'>{ret:+.2f}%</span>"
            pos = f"{b['position']:.4f}"
            px = f"${b['last_price']:,.2f}"
        else:
            equity = f"${b['initial_capital']:,.2f}"
            retc = "<span style='color:#93a1a1'>—</span>"
            pos = "—"
            px = "—"
        rows.append(
            f"<tr><td>{i}</td><td><b>{b['strategy']}</b></td>"
            f"<td>{b['symbol']}</td><td>{equity}</td><td>{retc}</td>"
            f"<td>{pos}</td><td>{px}</td></tr>")

    as_of = scoreboard.get("as_of") or "no ticks published yet"
    code = scoreboard.get("code", {})
    commit = (code.get("implementation_commit") or "")[:10] or "n/a"
    tree = (code.get("source_tree_sha256") or "")[:12]
    checkpoint = scoreboard.get("checkpoint") or "none published"

    banner = ("" if trading else
              "<div class='prep'>This experiment is <b>PREPARED</b> but not yet "
              "ACTIVE — activation is a separate, human-gated launch step. No live "
              "results are shown until it is activated and publishes a tick.</div>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>PREVIEW · Generation 2 scoreboard · {status}</title>
<!-- PREPARED PREVIEW — NOT DEPLOYED. Offline, self-contained. -->
<style>
  body{{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#002b36;
       color:#93a1a1;margin:0;padding:2rem;}}
  .wrap{{max-width:880px;margin:0 auto;}}
  h1{{color:#eee8d5;font-size:1.4rem;margin:0 0 .3rem;}}
  .badge{{display:inline-block;padding:.15rem .6rem;border-radius:.4rem;
          color:#002b36;font-weight:700;background:{badge[0]};}}
  .meta{{font-size:.8rem;color:#657b83;margin:.6rem 0 1.2rem;line-height:1.5;}}
  .gen1{{background:#3a1113;border:1px solid #dc322f;color:#eee8d5;
         padding:.7rem 1rem;border-radius:.5rem;margin:1rem 0;font-size:.85rem;}}
  .prep{{background:#123;border:1px solid #b58900;color:#eee8d5;padding:.7rem 1rem;
         border-radius:.5rem;margin:1rem 0;font-size:.85rem;}}
  table{{width:100%;border-collapse:collapse;margin-top:.5rem;}}
  th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #073642;
         font-variant-numeric:tabular-nums;}}
  th{{color:#eee8d5;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;}}
  code{{color:#839496;}}
</style></head>
<body><div class="wrap">
  <h1>📈 Paper-trading scoreboard · Generation 2</h1>
  <div><span class="badge">{badge[1]}</span></div>
  <div class="meta">
    experiment <code>{scoreboard['experiment_id']}</code><br/>
    code <code>{commit}</code> · tree <code>{tree}</code> ·
    ${scoreboard['capital_per_bot']:,.0f} per bot · {len(scoreboard['bots'])} bots ·
    checkpoint <code>{checkpoint}</code> ·
    as of {as_of}
  </div>
  <div class="gen1">⚠ {scoreboard['gen1_notice']}</div>
  {banner}
  <table>
    <thead><tr><th>#</th><th>Strategy</th><th>Coin</th><th>Equity</th>
      <th>Return</th><th>Position</th><th>Last price</th></tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</div></body></html>
"""
