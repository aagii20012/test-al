# Generation 1 Remediation — Final Report

**Status: `READY_FOR_GENERATION_2_APPROVAL`**

Every correctness gate is green, Generation 1 is frozen and labelled invalidated,
a single shared real data window is downloaded and hash-verified, and the
corrected engine has completed a clean falsification run over it. The one
remaining action — actually launching Generation 2 — is deliberately **not**
taken; it requires your explicit approval. Nothing here was committed, pushed,
deployed, or run against a real/testnet account.

> This status is **not** `ELIGIBLE_FOR_LIVE_VALIDATION`. Passing correctness
> gates and a paper falsification does not qualify any strategy for real money.
> See §9.

---

## 1. Authorization boundary — what was and was not done

The standing boundary was: *may implement changes, run local tests, and do
public keyless data downloads; may not launch Generation 2, reset active state,
modify/delete Gen1 evidence, place real or testnet orders, access credentials,
commit or push, or deploy the dashboard.*

| Allowed & done | Prohibited & NOT done |
|---|---|
| Code corrections (working tree only) | ❌ No commit — last commit is still `b617357`, unchanged |
| Local test runs (79 passed) | ❌ No push |
| Keyless public Coinbase downloads | ❌ No dashboard deploy (banner prepared, undeployed) |
| Froze Gen1 into new `evidence/` (additive) | ❌ No Gen2 launch (plan written, nothing started) |
| Corrected falsification backtest (simulated) | ❌ No state reset / no `state/*.json` touched |
| Prepared launch plan + dashboard banner | ❌ No real/testnet orders, no credentials read |
| | ❌ No Gen1 evidence overwritten; no synthetic candles |

Verified at report time: `git status` shows only working-tree modifications and
new untracked files; `state/` is clean; the last commit is unchanged.

---

## 2. What went wrong in Generation 1 (why it was invalidated)

Three defects made Gen1's numbers unusable and non-comparable:

1. **Position desync across restarts.** The cloud bot runs one decision cycle per
   process invocation ("tick"). Three strategies did not preserve required
   position state across those restarts, so a restarted bot could decide
   differently than an uninterrupted one — the recorded results were an artefact
   of restart timing, not the strategy.
2. **Reversal cost-basis defect.** A fill that flipped a position through zero
   left a stale cost basis, so the next reduce/close mismeasured realized P&L.
3. **Inconsistent evaluation windows.** The bots did not all share the same
   evaluation window, so their returns were not comparable to each other.

Full write-up: `evidence/gen1/INVALIDATION_REPORT.md`. The frozen Gen1 snapshot
set hashes to `03880ecd…82a8ff2e` (`evidence/gen1/MANIFEST.sha256`).

---

## 3. The five decisions and how each was implemented

| # | Decision | Implementation | Proof |
|---|---|---|---|
| 1 | **Portfolio-authoritative sync** — portfolio is the single source of truth; `_pos`/`_in_market` are overwritten from it at the last safe point (after all fills/forced exits/stops/breakers/pending orders, before signal calc) | `algotrading/engine/loop.py`, `strategy/base.py`, `strategy/momentum.py`, `cli.py` | `tests/test_restart_equivalence.py` |
| 2 | **Honest reversal accounting** — one real fill is never fabricated into two exchange fills; P&L books only on `min(|prev|,|fill|)`; residual opens at the actual fill price; a reversal is represented as linked CLOSE+OPEN legs sharing one parent fill id | `algotrading/portfolio/portfolio.py` | `tests/test_reversal_cost_basis.py`, `tests/test_audit_ledger.py` |
| 3 | **Fail-closed generation/schema boundary** — Gen1 (unmarked) and non-current schema are rejected on load; no migration/repair; fresh start needs explicit approval; no state file overwritten | `algotrading/state_schema.py`, `cli.py` | `tests/test_state_schema.py` |
| 4 | **Public dashboard invalidation** — additive banner (verbatim text), every Gen1 result labelled INVALIDATED, original numbers preserved; prepared, not deployed | `index.html` (working tree), `dashboard_preview.html` | Visual preview; `git status` shows undeployed |
| 5 | **Verified real data** — keyless Coinbase, deterministic pagination, content hash + manifest, strict validation, missing candles recorded not synthesised | `algotrading/data/history.py`, `tools/download_history.py`, `historical_data/gen2/` | `tests/test_history_download.py` |

---

## 4. The 11-step sequence — completion status

| Step | Description | Status |
|---|---|---|
| 1 | Freeze Gen1 evidence (immutable snapshot + manifest + hash) | ✅ Done |
| 2 | Invalidation report (verbatim banner + three defects) | ✅ Done |
| 3 | Generation/schema boundary (fail-closed) | ✅ Done + tested |
| 4 | Portfolio-authoritative sync | ✅ Done + tested |
| 5 | Reversal-accounting correction | ✅ Done + tested |
| 6 | Append-only fill/lifecycle-leg audit records | ✅ Done + tested |
| 7 | Historical-data pagination + validation + real download | ✅ Done + tested |
| 8 | Run full test gates | ✅ 79 passed |
| 9 | Prepare (not launch) Gen2 | ✅ `GEN2_LAUNCH_PLAN.md` |
| 10 | Prepare (not deploy) dashboard invalidation | ✅ banner + preview |
| 11 | Corrected historical falsification | ✅ `evidence/gen2/falsification/` |

---

## 5. Correctness gates (as of this report)

```
python -m pytest -q  →  79 passed in 10.20s
```

| Gate | File | Proves |
|---|---|---|
| Restart-equivalence | `tests/test_restart_equivalence.py` | Uninterrupted vs restart vs corrupted-cache restart are byte-identical bar-for-bar |
| Reversal cost-basis | `tests/test_reversal_cost_basis.py` | P&L booked only on closed qty; residual reopens at actual fill price |
| Audit ledger | `tests/test_audit_ledger.py` | One fill = one record + linked OPEN/CLOSE legs; a reversal is never two executions |
| Generation/schema | `tests/test_state_schema.py` | Gen1/non-current schema rejected; fresh start needs approval; Gen1 evidence never overwritten |
| Tick state round-trip | `tests/test_tick_state.py` | All cross-bar state persists; desync self-heals |
| Historical data | `tests/test_history_download.py` | Deterministic pagination; duplicate/unordered/non-hourly/incomplete rejected; missing candles recorded not synthesised |

---

## 6. Shared evaluation data (verified, real)

One shared, fully-closed, real 1-hour window for **all** strategies and symbols
— the fix for defect #3.

| Symbol | Window (UTC) | Candles | Missing | SHA-256 |
|---|---|---|---|---|
| BTCUSDT | 2025-07-01 → 2026-07-01 | 8750 / 8760 | 10 | `ef0c1e4f…950c948` |
| ETHUSDT | 2025-07-01 → 2026-07-01 | 8750 / 8760 | 10 | `b2d43b55…6a8be7d8` |

- Source: Coinbase public keyless candles, label **REAL**.
- The 10 missing candles per symbol are two genuine 5-hour Coinbase outages
  (2025-10-25 16:00–20:00Z and 2026-05-08 02:00–06:00Z), identical across both
  products — recorded in the manifests, **never synthesised** (0.11% of the
  window, treated as disclosed bar gaps).
- The falsification runner **re-hashes the raw bytes and aborts on any mismatch**
  before running, so the numbers below are provably from this exact data.

---

## 7. Falsification results (corrected engine, shared window)

Four candidate strategies × two coins = eight independent simulated bots, one
fixed default parameter set each, identical cost/risk model
(commission 0.001, slippage 2 bps, fill at close, $10 min-notional,
`config.ci` risk plan, $10,000 start). Full detail:
`evidence/gen2/falsification/{FALSIFICATION_REPORT.md,results.json}`.

| Rank | Strategy | Coin | Net return | Sharpe | Max DD | Trades | Final equity |
|---|---|---|---|---|---|---|---|
| 1 | momentum | BTCUSDT | **+4.90%** | +0.69 | −7.41% | 89 | $10,489.93 |
| 2 | momentum | ETHUSDT | +1.05% | +0.18 | −12.19% | 97 | $10,105.20 |
| 3 | donchian | ETHUSDT | −5.82% | −0.42 | −15.29% | 105 | $9,418.47 |
| 4 | rsi | ETHUSDT | −8.90% | −2.54 | −9.79% | 204 | $9,110.05 |
| 5 | donchian | BTCUSDT | −11.49% | −1.22 | −15.13% | 94 | $8,850.74 |
| 6 | rsi | BTCUSDT | −11.50% | −3.50 | −13.01% | 214 | $8,849.86 |
| 7 | bollinger | BTCUSDT | −14.88% | −3.40 | −15.06% | 143 | $8,511.82 |
| 8 | bollinger | ETHUSDT | −16.18% | −2.92 | −16.18% | 121 | $8,381.91 |

**Notable:** on this shared, verified window with the corrected engine, only
momentum finished positive on either coin. Bollinger — which topped the
*invalidated* Gen1 leaderboard — finished last on both coins. That inversion is
itself evidence that the Gen1 ranking could not be trusted. Momentum (the live
candidate) is the only strategy positive on both assets, but on a thin
edge (+4.90% / +1.05%) against real costs.

---

## 8. Ledger integrity (with an honest caveat)

Every bot's `fills` count equals its real fill count; each fill emits exactly one
lifecycle leg (CLOSE on reduce/close, OPEN on establish/add); `realized_pnl` is
booked only on the closed quantity at the pre-fill basis.

**`reversals = 0` for all eight bots.** That is why `legs == fills` exactly here:
under this risk config the stops and daily halts always flatten a position to
zero *before* an opposite signal opens a new one, so no single fill crosses
through zero. The through-zero reversal path (the CLOSE+OPEN split from one fill)
is therefore **proven by `tests/test_reversal_cost_basis.py`, not exercised by
this run.** The corrected mechanism exists and is tested; these particular
strategy/parameter/window combinations simply never triggered it. Stated plainly
so the result is not over-read.

---

## 9. What is prepared but NOT done — and why it needs you

| Prepared | Blocked on | Where |
|---|---|---|
| Generation 2 launch procedure | Your explicit per-launch approval | `GEN2_LAUNCH_PLAN.md` |
| Dashboard invalidation banner | Your approval to deploy (= commit + push) | `index.html` (working tree), `dashboard_preview.html` |

**Not eligible for live validation.** Correct code, verified data, and a passing
paper falsification are necessary but **not sufficient** for real money. Live
validation is a separate decision with its own evidence bar (out-of-sample
windows, more assets, walk-forward parameter stability, live-fill/slippage
realism, risk sign-off) and is explicitly out of scope here.

---

## 10. Recommended next decision

The remediation is complete and the boundary is intact. Your call, one of:

1. **Approve Generation 2 launch** (simulated paper only) per
   `GEN2_LAUNCH_PLAN.md` — I run the first ticks with the fresh-generation flag,
   then hand back to the scheduler.
2. **Approve dashboard deploy** — commit + push the invalidation banner so the
   public scoreboard stops implying the Gen1 ranking is meaningful.
3. **Neither yet** — hold while you review this report and the evidence.

I will not take any of these without your explicit go-ahead.
