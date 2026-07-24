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
from datetime import datetime, timezone
from typing import Optional

from . import checkpoint as cp

GEN1_NOTICE = (
    "Generation 1 is INVALIDATED (position-desync + reversal cost-basis defects). "
    "Its results are frozen forensic evidence and are NOT shown here."
)

# "Simulated paper trading" disclaimer shown on every deployed page. Non-negotiable
# (Message B §6): the public dashboard must never read as real-money trading and
# must never use "validated"/"recommended" language.
PAPER_DISCLAIMER = (
    "Simulated paper trading — no real money. Positions, equity and returns are "
    "hypothetical results computed against public price candles; nothing here is "
    "traded on an exchange, and none of it is investment advice or a recommendation."
)

# Data-age threshold for the deployed dashboard. Candles are 1h and (once the
# recurring coordinator is approved) a tick publishes hourly, so a healthy board
# refreshes within ~1h. Past this bound the page shows a prominent stale-data
# warning instead of implying the numbers are current. During the manual-canary
# phase there is NO recurring schedule, so the board is EXPECTED to read stale
# shortly after the one canary tick — that is honest, not a fault.
STALE_AFTER_S = 2 * 3600 + 900  # 2h15m: one hourly cadence + slack


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


# --------------------------------------------------------------------------
# Deployed (GitHub Pages) dashboard
#
# This is the public, deployed variant of the board. It differs from the offline
# preview above in ways Message B §6 requires of anything served to the public:
#   * a permanent "simulated paper trading — no real money" disclaimer;
#   * the last successful decision boundary (UTC) and the AGE of that data;
#   * a prominent stale-data warning once the data is older than STALE_AFTER_S;
#   * no "PREVIEW / NOT DEPLOYED" language and no "validated"/"recommended" claim.
# It still reads ONLY the verified CURRENT checkpoint (via build_scoreboard) and
# still fails closed on a corrupt store.
# --------------------------------------------------------------------------
def _fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render_pages_html(scoreboard: dict, *, now: Optional[datetime] = None,
                      stale_after_s: int = STALE_AFTER_S) -> str:
    """Deployment-grade HTML for the public GitHub Pages board.

    ``now`` is injectable for deterministic tests; it defaults to the current UTC
    time. Data age is measured from the published decision boundary
    (``decision_epoch_ms``); when that is older than ``stale_after_s`` a stale
    warning is shown. A PREPARED experiment (no published tick) renders an
    explicit "no tick published yet" state rather than fake numbers.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    status = scoreboard["status"]
    trading = scoreboard["trading"]

    badge = {
        "PREPARED": ("#b58900", "PREPARED — built &amp; bound, not trading yet"),
        "ACTIVE": ("#2aa198", "ACTIVE — simulated paper trading"),
        "PAUSED": ("#cb4b16", "PAUSED"),
        "FAILED": ("#dc322f", "FAILED — a tick aborted; under investigation"),
        "FAILED_CANARY": ("#dc322f", "FAILED_CANARY — retired"),
        "CLOSED": ("#586e75", "CLOSED"),
    }.get(status, ("#586e75", status))

    # Boundary + data age come only from a live, verified checkpoint.
    epoch_ms = scoreboard.get("decision_epoch_ms")
    boundary_utc = "no tick published yet"
    age_txt = "—"
    stale = False
    if trading and epoch_ms:
        boundary_dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=timezone.utc)
        boundary_utc = boundary_dt.strftime("%Y-%m-%d %H:%M UTC")
        age_s = (now - boundary_dt).total_seconds()
        age_txt = _fmt_age(age_s)
        stale = age_s > stale_after_s

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

    code = scoreboard.get("code", {})
    commit = (code.get("implementation_commit") or "")[:10] or "n/a"
    tree = (code.get("source_tree_sha256") or "")[:12]
    checkpoint = scoreboard.get("checkpoint") or "none published"
    generated = now.strftime("%Y-%m-%d %H:%M UTC")

    if not trading:
        banner = ("<div class='prep'>This experiment is <b>{s}</b>. No live results "
                  "are shown until it is activated and publishes a tick. Activation "
                  "is a separate, human-gated step.</div>").format(s=status)
    else:
        banner = ""

    stale_banner = ""
    if stale:
        stale_banner = (
            "<div class='stale'>⚠ <b>Stale data.</b> The most recent decision "
            f"boundary is <b>{age_txt}</b> old (published {boundary_utc}). This "
            "experiment is a manual canary and is <b>not on a recurring schedule</b> "
            "yet, so these numbers are a point-in-time snapshot and are not being "
            "refreshed. Do not read them as current.</div>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex"/>
<title>Generation 2 paper-trading scoreboard · {status}</title>
<!-- Static, self-contained. Rendered from the committed CURRENT checkpoint only.
     Contains NO trading logic and makes no network calls. -->
<style>
  body{{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#002b36;
       color:#93a1a1;margin:0;padding:2rem;}}
  .wrap{{max-width:880px;margin:0 auto;}}
  h1{{color:#eee8d5;font-size:1.4rem;margin:0 0 .3rem;}}
  .badge{{display:inline-block;padding:.15rem .6rem;border-radius:.4rem;
          color:#002b36;font-weight:700;background:{badge[0]};}}
  .disclaimer{{background:#073642;border:1px solid #268bd2;color:#eee8d5;
       padding:.7rem 1rem;border-radius:.5rem;margin:1rem 0;font-size:.9rem;}}
  .meta{{font-size:.8rem;color:#657b83;margin:.6rem 0 1.2rem;line-height:1.6;}}
  .gen1{{background:#3a1113;border:1px solid #dc322f;color:#eee8d5;
         padding:.7rem 1rem;border-radius:.5rem;margin:1rem 0;font-size:.85rem;}}
  .prep{{background:#123;border:1px solid #b58900;color:#eee8d5;padding:.7rem 1rem;
         border-radius:.5rem;margin:1rem 0;font-size:.85rem;}}
  .stale{{background:#4a2c05;border:1px solid #cb4b16;color:#eee8d5;padding:.7rem 1rem;
         border-radius:.5rem;margin:1rem 0;font-size:.9rem;}}
  table{{width:100%;border-collapse:collapse;margin-top:.5rem;}}
  th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #073642;
         font-variant-numeric:tabular-nums;}}
  th{{color:#eee8d5;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;}}
  code{{color:#839496;}}
  .foot{{margin-top:1.5rem;font-size:.72rem;color:#586e75;}}
</style></head>
<body><div class="wrap">
  <h1>📈 Paper-trading scoreboard · Generation 2</h1>
  <div><span class="badge">{badge[1]}</span></div>
  <div class="disclaimer">💵 {PAPER_DISCLAIMER}</div>
  <div class="meta">
    experiment <code>{scoreboard['experiment_id']}</code><br/>
    code <code>{commit}</code> · tree <code>{tree}</code> ·
    ${scoreboard['capital_per_bot']:,.0f} per bot · {len(scoreboard['bots'])} bots<br/>
    checkpoint <code>{checkpoint}</code><br/>
    last successful boundary <b>{boundary_utc}</b> · data age <b>{age_txt}</b>
  </div>
  {stale_banner}
  <div class="gen1">⚠ {scoreboard['gen1_notice']}</div>
  {banner}
  <table>
    <thead><tr><th>#</th><th>Strategy</th><th>Coin</th><th>Equity</th>
      <th>Return</th><th>Position</th><th>Last price</th></tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
  <div class="foot">Static page generated {generated} from the committed CURRENT
    checkpoint. No trading logic runs here; the page makes no network calls.</div>
</div></body></html>
"""


def render_pages_placeholder_html(*, now: Optional[datetime] = None) -> str:
    """Honest deployed page for when NO eligible Generation-2 experiment exists.

    Used during the dormant remediation phase (only a retired/terminal experiment
    is on disk) so the public site never implies a board is live, yet still shows
    the paper-trading disclaimer and the standing Generation-1 INVALIDATED notice.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex"/>
<title>Generation 2 paper-trading scoreboard · no active experiment</title>
<style>
  body{{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#002b36;
       color:#93a1a1;margin:0;padding:2rem;}}
  .wrap{{max-width:880px;margin:0 auto;}}
  h1{{color:#eee8d5;font-size:1.4rem;margin:0 0 .3rem;}}
  .disclaimer{{background:#073642;border:1px solid #268bd2;color:#eee8d5;
       padding:.7rem 1rem;border-radius:.5rem;margin:1rem 0;font-size:.9rem;}}
  .gen1{{background:#3a1113;border:1px solid #dc322f;color:#eee8d5;
         padding:.7rem 1rem;border-radius:.5rem;margin:1rem 0;font-size:.85rem;}}
  .prep{{background:#123;border:1px solid #b58900;color:#eee8d5;padding:.7rem 1rem;
         border-radius:.5rem;margin:1rem 0;font-size:.9rem;}}
  .foot{{margin-top:1.5rem;font-size:.72rem;color:#586e75;}}
</style></head>
<body><div class="wrap">
  <h1>📈 Paper-trading scoreboard · Generation 2</h1>
  <div class="disclaimer">💵 {PAPER_DISCLAIMER}</div>
  <div class="prep">There is <b>no active Generation-2 experiment</b> right now.
    A prior canary was retired under a source-binding portability defect and a
    fresh experiment has not yet been activated. Nothing is trading.</div>
  <div class="gen1">⚠ {GEN1_NOTICE}</div>
  <div class="foot">Static page generated {generated}. No trading logic runs here;
    the page makes no network calls.</div>
</div></body></html>
"""


def build_pages_site(exp_dir: str, out_dir: str, *, now: Optional[datetime] = None,
                     stale_after_s: int = STALE_AFTER_S) -> dict:
    """Render the deployed dashboard for ONE experiment into ``out_dir``.

    Writes ``<out_dir>/index.html`` and an empty ``<out_dir>/.nojekyll`` (so Pages
    serves the file verbatim without Jekyll processing). Returns the scoreboard.

    Fail-closed: :func:`build_scoreboard` resolves + verifies CURRENT and raises on
    any integrity defect. The caller (the Pages workflow) must let that propagate
    so the deploy step is skipped and the last-good published site is preserved,
    rather than replacing it with a page built from unverifiable state.
    """
    sb = build_scoreboard(exp_dir)
    html = render_pages_html(sb, now=now, stale_after_s=stale_after_s)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(html)
    # Disable Jekyll so files/underscored paths are served as-is.
    with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("")
    return sb
