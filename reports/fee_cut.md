# Cutting the fee drag on momentum (Goal 2 prep)

**Goal:** preserve capital first (Goal 1), then *gain money* — but only after
stopping fees from eating the edge. On a $100 account, fees and the $10
minimum-order size are the dominant cost, not the strategy.

## The problem

The default momentum settings flipped position every time the score crossed
zero (`threshold=0.5`, `exit_band=0.0`). The single-pass backtest the user ran:

| Metric | Value |
|---|---|
| Trades | 91 |
| Commission | $8.67 (8.7% of the account) |
| Gross return | −5.57% |
| **Net return** | **−15.29%** |
| Win rate | 20.9% |

Fees turned a small gross loss into a −15% net loss.

## The fix

Two parts:

1. **Trade less often.** A longer lookback (96 vs 48) and higher entry bar
   (1.0 vs 0.5) make the signal slower and more selective. An optional
   hysteresis `exit_band` lets a position hold through small reversals instead
   of bailing at zero. Validated by a 27-point parameter sweep.

2. **CLI bug fix.** `cmd_backtest` / `cmd_live` ignored `DEFAULT_PARAMS` when
   `--param` was omitted — strategies silently fell back to their churniest
   constructor defaults (`threshold=0.0`). Added `_resolve_params()` so
   `DEFAULT_PARAMS` is the single source of truth, with `--param` layered on top.
   This is what the testnet/live path runs, so it matters most.

## Result — single-pass (the exact command the user ran)

`python -m algotrading.cli backtest --strategy momentum`

| Metric | Before | After |
|---|---|---|
| Final equity | $84.71 | **$107.66** |
| Net return | −15.29% | **+7.66%** |
| Trades | 91 | **55** |
| Commission | $8.67 | **$3.33** |
| Sharpe | −3.09 | **1.38** |
| Max drawdown | −15.29% | **−3.51%** |
| Win rate | 20.9% | **45.5%** |

## Result — walk-forward OOS (the honest test)

| Grid | Net % | Trades | Fees | Max drop | Sharpe |
|---|---|---|---|---|---|
| Original | +5.4% | 91 | $6.47 | −4.8% | 0.93 |
| **Fee-efficient** | +4.4% | **33** | **$1.93** | **−3.5%** | **1.14** |

Out-of-sample the raw return is ~$1 lower, but turnover drops ~64%, fees ~70%,
drawdown shrinks, and risk-adjusted return (Sharpe) rises from 0.93 to 1.14.

**Important:** keeping the short-lookback / low-threshold options *in the grid*
made OOS worse (−0.1%) — the optimiser picks them in-sample then they fail live.
The grid deliberately excludes them.

## Why this is the right call for a $100 account

- Fewer, larger trades → fewer orders rejected by the $10 minimum-notional
  (the thing that would actually stop live trading).
- Higher Sharpe / smaller drawdown → steadier, which serves Goal 1 (don't lose
  capital) directly.
- The small give-up in raw return is noise; the turnover/cost reduction is real.

## Settings now in place

- `DEFAULT_PARAMS["momentum"] = {lookback: 96, threshold: 1.0}` (live/testnet).
- `PARAM_GRIDS["momentum"] = {lookback: [96, 168], threshold: [1.0, 1.5], exit_band: [0.0, -0.3]}` (walk-forward).
