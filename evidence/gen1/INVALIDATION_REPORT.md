# Generation 1 — Invalidation Report

**Status: `INVALIDATED`**
**Generation: `gen1`**
**Report prepared: 2026-07-23 (UTC)**

Generation 1 is the crypto paper-trading bake-off recorded in `state/*.json`
(momentum, rsi, donchian, bollinger × BTCUSDT, ETHUSDT), run hourly by
`.github/workflows/paper-trade.yml` in `tick --simulated` mode. It is invalidated
by three independent defects that make its recorded results unsound. This
document is the permanent forensic record of *why*. The underlying state files
are preserved unchanged and hashed in [`MANIFEST.json`](MANIFEST.json) /
[`MANIFEST.sha256`](MANIFEST.sha256); frozen byte-exact copies live in
[`state_snapshot/`](state_snapshot/).

---

## Public banner text (verbatim)

The wording below is the exact, non-negotiable text placed on the public
dashboard. It does **not** erase or rewrite the original numbers; those remain as
invalidated forensic records.

> **Generation 1 is invalidated. Results are not comparable and must not be used
> to select a strategy. Three strategies failed to preserve required position
> state across restarts, a reversal cost-basis defect affected accounting, and
> the bots did not all share the same evaluation window.**

---

## Defect 1 — Position state was not preserved across restarts (3 of 4 strategies)

The bot runs in `tick` mode: **one decision cycle per process invocation.** The
process starts, loads its JSON checkpoint, makes at most one decision, saves, and
exits. Any position memory a strategy holds in Python attributes therefore has to
survive that round-trip *and* be reconciled against the real book — otherwise the
strategy's idea of "am I in the market?" silently diverges from the portfolio.

Only **momentum** implemented the required behaviour (it persisted `_pos` and
reconciled it via `sync_positions`). The other three bake-off strategies did
not — verified directly from the source:

| Strategy   | Position memory | Persisted across ticks? | Reconciled to book? | Effect |
|------------|-----------------|-------------------------|---------------------|--------|
| momentum   | `_pos` (int)    | yes                     | yes                 | (protected) |
| **rsi**    | `_in_market` (bool) | **no**              | **no**              | wedges |
| **donchian** | `_pos` (int)  | **no**                  | **no**              | wedges |
| **bollinger** | `_pos` (int) | **no**                  | **no**              | wedges |

Because a fresh process starts each of the three vulnerable strategies with an
empty/zeroed position memory, and nothing re-synced it from the portfolio, their
in-market flag did not reflect reality. A strategy that had opened a position (or
been flattened by a stop / circuit-breaker on a prior tick) could believe the
opposite on the next tick — entering when it thought it was flat, or refusing to
re-enter because it still "thought" it was in a trade it no longer held. Every
decision after the first divergence is drawn from corrupted state.

This is a **restart-equivalence** failure: an uninterrupted run and a
restart-every-candle run did not produce the same decisions. For a cron bot,
restart-every-candle *is* the normal operating mode, so the recorded decisions
are not the decisions the strategies' own logic would have made.

## Defect 2 — Reversal cost-basis accounting defect

When a single fill reverses a position through zero (e.g. long → short in one
execution), the surviving residual position on the opposite side must open at the
**actual fill price**. The Generation 1 accounting had no branch for crossing
through zero, so after a reversal the residual position kept the *closed* leg's
stale average price as its cost basis. Every subsequent unrealized- and
realized-P&L figure for that position was computed against a wrong basis until the
position was next fully closed. This distorts equity curves and per-trade P&L
wherever a reversal occurred.

## Defect 3 — Bots did not share one evaluation window

The eight bots did not all evaluate over the same set of candles / same time
window. Cross-strategy and cross-coin comparisons therefore compare results
measured over different market conditions. A leaderboard built on non-aligned
windows cannot rank strategies fairly.

---

## Consequence

The three defects are independent and each is individually sufficient to
invalidate the leaderboard:

* **Defect 1** corrupts the *decisions* of 3 of the 4 strategies.
* **Defect 2** corrupts the *accounting* wherever a reversal occurred.
* **Defect 3** corrupts the *comparison* even where individual accounting is
  right.

Therefore **no Generation 1 result may be used to select, rank, or promote a
strategy.** The numbers are retained only as invalidated forensic records.

## What is NOT being done to Gen1

* Gen1 state files are **not** modified, migrated, repaired, or reconstructed.
* No inferred cost-basis reconstruction is performed on Gen1.
* Gen1 is **not** loaded into the corrected (Generation 2) runner — the corrected
  runner fails closed on any non-Generation-2 state (see the generation boundary).
* The public numbers are **not** erased; an invalidation banner is added and every
  result is labelled `INVALIDATED`.

## Evidence integrity

All eight state files are hashed (SHA-256) in `MANIFEST.json`. The set as a whole
is covered by `set_sha256 = 03880ecdeca7f30b4c1c43381f84102ac20608a80addbb3da2a9786e82a8ff2e`.
Frozen copies in `state_snapshot/` reproduce the originals byte-for-byte and were
verified to hash-match at freeze time.
