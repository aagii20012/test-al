# Generation 2 — Corrected Historical Falsification

> Corrected engine (portfolio-authoritative sync + reversal cost-basis fix + append-only audit ledger) over a single shared, hash-verified, REAL price window. This is a backtest for falsification only — **not** a Gen2 launch and **not** a basis for live validation.

## Data (hash-verified before use)

| Symbol | Window | Bars (actual/expected) | Missing | SHA-256 |
|---|---|---|---|---|
| BTCUSDT | 2025-07-01T00:00:00+00:00 → 2026-07-01T00:00:00+00:00 | 8750/8760 | 10 | `ef0c1e4f3b71ca79…` |
| ETHUSDT | 2025-07-01T00:00:00+00:00 → 2026-07-01T00:00:00+00:00 | 8750/8760 | 10 | `b2d43b552186cc2d…` |

Missing hours are genuine source gaps, recorded and **not** synthesised; both symbols share the identical calendar so the union timeline simply has no bar at those hours.

## Cost & risk model (identical across all bots)

- Commission: 0.001 (per fill notional)
- Slippage: 2.0 bps
- Fill: at bar close; min notional $10.0
- Initial capital: $10,000; financing APR 0.1
- Risk: config/config.ci.yaml (ATR sizing, 1%/trade, 5% stop, 1% daily loss/profit halts, 15% max-drawdown kill switch)

## Results (ranked by net return)

| Rank | Strategy | Coin | Net return | Sharpe | Max DD | Trades | Fills | Legs | Reversals | Final equity |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | momentum | BTCUSDT | +4.90% | +0.69 | -7.41% | 89 | 178 | 178 | 0 | $10,489.93 |
| 2 | momentum | ETHUSDT | +1.05% | +0.18 | -12.19% | 97 | 194 | 194 | 0 | $10,105.20 |
| 3 | donchian | ETHUSDT | -5.82% | -0.42 | -15.29% | 105 | 210 | 210 | 0 | $9,418.47 |
| 4 | rsi | ETHUSDT | -8.90% | -2.54 | -9.79% | 204 | 409 | 409 | 0 | $9,110.05 |
| 5 | donchian | BTCUSDT | -11.49% | -1.22 | -15.13% | 94 | 189 | 189 | 0 | $8,850.74 |
| 6 | rsi | BTCUSDT | -11.50% | -3.50 | -13.01% | 214 | 429 | 429 | 0 | $8,849.86 |
| 7 | bollinger | BTCUSDT | -14.88% | -3.40 | -15.06% | 143 | 286 | 286 | 0 | $8,511.82 |
| 8 | bollinger | ETHUSDT | -16.18% | -2.92 | -16.18% | 121 | 242 | 242 | 0 | $8,381.91 |

## Ledger integrity

Every bot's `fills` count equals its real fill count; each fill emits exactly
one lifecycle `leg` — a CLOSE when it reduces/closes a position, an OPEN when it
establishes/adds one. `realized_pnl` is booked only on the closed quantity at the
pre-fill cost basis (the corrected accounting).

**`reversals = 0` for all eight bots**, and this is why `legs` equals `fills`
exactly (one leg per fill): under this risk config the stops and daily halts
always flatten a position to zero *before* the opposite signal opens a new one,
so no single fill crosses through zero. A through-zero reversal is the only case
that would split one fill into two legs (CLOSE + OPEN sharing the parent
`fill_id`). That reversal-decomposition path is therefore **proven by the unit
test `tests/test_reversal_cost_basis.py`, not exercised by this run** — an honest
distinction: the corrected mechanism exists and is tested, but these particular
strategy/parameter/window combinations never triggered it.

| Strategy | Coin | Realized P&L | Fills | Legs | Reversals |
|---|---|---|---|---|---|
| momentum | BTCUSDT | $1,036.09 | 178 | 178 | 0 |
| momentum | ETHUSDT | $505.68 | 194 | 194 | 0 |
| donchian | ETHUSDT | $330.00 | 210 | 210 | 0 |
| rsi | ETHUSDT | $-538.55 | 409 | 409 | 0 |
| donchian | BTCUSDT | $-215.56 | 189 | 189 | 0 |
| rsi | BTCUSDT | $-605.16 | 429 | 429 | 0 |
| bollinger | BTCUSDT | $-403.38 | 286 | 286 | 0 |
| bollinger | ETHUSDT | $-992.83 | 242 | 242 | 0 |

## Interpretation

- These numbers are trustworthy in a way Gen1's were not: one shared verified window, corrected accounting, auditable ledger.
- They are a single fixed-parameter backtest over one 12-month window on two correlated assets — a falsification probe, not an out-of-sample validation and not a live result.
- Passing this step does **not** qualify any strategy for live/real-money validation.
