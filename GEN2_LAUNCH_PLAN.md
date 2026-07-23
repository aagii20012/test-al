# Generation 2 — Launch Plan (PREPARED, NOT LAUNCHED)

> **Status: NOT LAUNCHED.** This document describes *how* Generation 2 would be
> started once launch is explicitly approved. Nothing in this file starts a run.
> No Generation 2 state file has been created. No scheduler/workflow has been
> enabled. Per the standing authorization boundary, launching Generation 2,
> resetting active state, placing real or testnet orders, and enabling the
> dashboard/workflows all remain **out of scope until separately approved**.

## 1. Why there is a Generation 2 at all

Generation 1 was invalidated (see `evidence/gen1/INVALIDATION_REPORT.md`). Three
defects made its numbers unusable and non-comparable:

1. **Position desync across restarts** — three strategies did not preserve
   required position state across the per-cycle process restarts of the cloud
   "tick" bot, so a restarted bot could decide differently than an uninterrupted
   one.
2. **Reversal cost-basis error** — a fill that reversed a position through zero
   left a stale cost basis, so the next reduce/close mis-measured realized P&L.
3. **Inconsistent evaluation windows** — the bots did not all share the same
   evaluation window.

Generation 2 exists to re-run the experiment on **corrected code** with a
**single shared, verified data window** and a **fail-closed generation boundary**
so Gen1 and Gen2 can never be mixed.

## 2. What has already been fixed and proven (gates)

Launch is gated on these being green (they are, as of this plan):

| Gate | Where | Proves |
|---|---|---|
| Restart-equivalence property | `tests/test_restart_equivalence.py` | Uninterrupted vs restart vs corrupted-cache restart are byte-identical bar-for-bar (Decision 1) |
| Reversal cost-basis | `tests/test_reversal_cost_basis.py` | P&L booked only on the closed qty; residual reopens at the actual fill price (Decision 2) |
| Append-only audit ledger | `tests/test_audit_ledger.py` | One fill = one record + linked OPEN/CLOSE legs; a reversal is never two executions (Decision 2) |
| Generation/schema boundary | `tests/test_state_schema.py` | Gen1 (unmarked) and non-current schema are rejected; fresh start needs approval; Gen1 evidence never overwritten (Decision 3) |
| Tick state round-trip | `tests/test_tick_state.py` | All cross-bar state persists and desync self-heals |
| Historical data validation | `tests/test_history_download.py` | Deterministic pagination; duplicate/unordered/non-hourly/incomplete rejected; missing candles recorded, never synthesised (Decision 5) |

Run all gates:

```bash
python -m pytest -q
```

Full suite must be green (currently **79 passed**). Do not launch on any red.

## 3. Generation identity (fail-closed boundary)

- Corrected state carries `generation = "gen2"`, `schema_version = 2`
  (`algotrading/state_schema.py`).
- Gen1 files carry **no** marker and are rejected on load with a clear
  incompatibility error. There is **no migration/repair path**.
- The runner refuses to create a fresh state unless launch is explicitly
  approved via `--allow-fresh-generation` (or `ALGOTRADING_ALLOW_FRESH_GEN2=1`).
- Gen1 evidence in `evidence/gen1/` is immutable (frozen snapshot set hash
  `03880ecd…82a8ff2e`) and must never be overwritten.

## 4. Shared evaluation data (verified, real)

One shared, fully-closed, real 1-hour window for **all** strategies and symbols
— this is the fix for defect #3.

| Symbol | Product | Window (UTC, half-open) | Candles | Missing | SHA-256 of raw snapshot |
|---|---|---|---|---|---|
| BTCUSDT | BTC-USD | 2025-07-01T00:00 → 2026-07-01T00:00 | 8750 / 8760 | 10 | `ef0c1e4f3b71ca7918e8d1a4b96b54437b14be9d1aa3f1a54beb31943950c948` |
| ETHUSDT | ETH-USD | 2025-07-01T00:00 → 2026-07-01T00:00 | 8750 / 8760 | 10 | `b2d43b552186cc2d4b395df6f2935a0b823385f8a14b6e4e6f886a276a8be7d8` |

- Source: Coinbase public key-less candles (`coinbase-public`), label **REAL**.
- Raw + manifest: `historical_data/gen2/{BTCUSDT,ETHUSDT}_1h_real.{raw,manifest}.json`.
- The 10 missing candles per symbol are two genuine 5-hour Coinbase outages
  (2025-10-25 16:00–20:00 and 2026-05-08 02:00–06:00), **identical across both
  products** — recorded in the manifests, never synthesised. This is 0.11% of the
  window; the falsification run treats these as bar gaps (disclosed), it does not
  fabricate prices.
- Reproduce/verify: `python -m tools.download_history` then re-hash the raw files.

## 5. Candidate strategies & parameters

Same candidates as Gen1, one fixed parameter set each (single-shot defaults from
`algotrading/research/grids.py:DEFAULT_PARAMS`), run on the shared window:

| Strategy | Params |
|---|---|
| momentum | `lookback=96 threshold=1.0` (chosen live candidate) |
| bollinger | `window=20 entry_z=2.0 exit_z=0.5` |
| rsi | `period=14 oversold=30 exit_level=50` |
| donchian | `entry=55 exit=20 trend=100` |

Capital, commission (0.001), slippage (2 bps), and risk config must be held
identical across all bots so results are comparable.

## 6. Launch procedure (RUN ONLY AFTER SEPARATE APPROVAL)

Generation 2 is a **simulated** paper run (no account, no orders). It never uses
`live`; it uses `tick --simulated`.

1. **Confirm gates green** (§2) and **do not proceed on any failure.**
2. **Confirm no stale state**: the target `state/*_sim.json` paths must not exist
   (a fresh experiment starts from a fresh, explicitly-initialised portfolio).
   Never delete or overwrite Gen1 evidence to make room.
3. **First tick per bot** (creates the fresh gen2 state; approval flag required):

   ```bash
   python -m algotrading.cli tick --simulated \
     --strategy momentum --symbols BTCUSDT --interval 1h \
     --param lookback=96 threshold=1.0 \
     --allow-fresh-generation
   ```

   Repeat for each (strategy, symbol) pair. The flag is required only on the
   first run; subsequent ticks resume the now-current gen2 state and must run
   **without** the flag (so a scheduler can never silently start a new one).
4. **Subsequent ticks** (scheduler or manual), no fresh-start flag:

   ```bash
   python -m algotrading.cli tick --simulated \
     --strategy momentum --symbols BTCUSDT --interval 1h \
     --param lookback=96 threshold=1.0
   ```

5. **Verify** each new state file carries `generation="gen2"`,
   `schema_version=2`, and that the append-only `fills`/`legs` ledger populates.

## 7. Hard constraints (do not violate)

- **Simulated only.** No real or testnet orders. Never pass
  `--i-understand-real-money`. No credentials are read.
- **No overwrite of Gen1.** Evidence and any existing state remain untouched;
  the save path refuses to overwrite a non-current (Gen1) file.
- **No synthetic candles.** Only the verified real snapshot (§4) is used.
- **Approval is per-launch.** This plan does not constitute approval to run.
- **Dashboard/workflows stay off** until separately approved (see the prepared,
  undeployed invalidation banner).

## 8. Not eligible for live validation

Passing correctness gates and completing a Gen2 paper run does **not** by itself
qualify any strategy for live/real-money validation. That is a separate decision
with its own evidence bar, explicitly out of scope here.
