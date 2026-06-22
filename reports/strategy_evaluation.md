# Strategy Evaluation & 10%/day Reality Check

- Costs: commission 0.10%/side, slippage 2.0 bps
- Capital: $100,000; bars: hourly; data: real BTCUSDT 1h + 4 synthetic regimes
- Risk policy: ATR sizing (2% risk/trade, 2.5-ATR stop), max 50% position, 5% daily-loss halt, 25% max-drawdown kill switch
- Walk-forward: train 2160 bars / test 720 bars, re-optimised by Sharpe each window


## 1. Regime stress test (default params, managed risk, with costs)


### Regime: bull

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|
| sma (trend-following)      |       15.72 |      0.0976 |     1.36 |      1.67 |    -13.73 |     2.75 |    48.3 | 1.51 |       29 |
| rsi (mean-reversion)       |        0.28 |      0.0017 |     0.34 |      0.18 |     -1.26 |     0.49 |    68.3 | 1.23 |       60 |
| bollinger (mean-reversion) |      -13.34 |     -0.0847 |    -3.14 |     -2.99 |    -16.7  |    -1.61 |    63.2 | 0.76 |      163 |
| donchian (breakout)        |      -17.54 |     -0.11   |    -1.76 |     -1.87 |    -23.16 |    -1.49 |    34.2 | 0.57 |       38 |
| momentum (momentum)        |       -3.36 |     -0.0186 |    -0.54 |     -0.78 |     -7.9  |    -0.91 |    33   | 0.96 |       88 |
| buyhold (benchmark)        |       -2.24 |     -0.0189 |    -1.15 |     -0.19 |     -4.34 |    -1.12 |     0   | 0    |        1 |

### Regime: bear

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|
| sma (trend-following)      |      -20.5  |     -0.1321 |    -2.25 |     -2.32 |    -22.5  |    -1.75 |    26.7 | 0.4  |       30 |
| rsi (mean-reversion)       |       -2.5  |     -0.0152 |    -2.19 |     -1.35 |     -3.36 |    -1.61 |    54.1 | 0.64 |       74 |
| bollinger (mean-reversion) |      -15.69 |     -0.1015 |    -3.74 |     -3.61 |    -18.55 |    -1.68 |    56   | 0.67 |      168 |
| donchian (breakout)        |      -24.8  |     -0.1667 |    -3    |     -2.72 |    -25.04 |    -1.85 |    26.7 | 0.32 |       30 |
| momentum (momentum)        |      -16.47 |     -0.1068 |    -3.35 |     -4.66 |    -16.67 |    -1.95 |    25.5 | 0.47 |       94 |
| buyhold (benchmark)        |       -2.23 |     -0.0151 |    -1.23 |     -0.16 |     -4.03 |    -1.19 |     0   | 0    |        1 |

### Regime: chop

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|
| sma (trend-following)      |      -24.71 |     -0.1691 |    -4.5  |     -3.13 |    -25.1  |    -1.84 |     5   | 0.01 |       20 |
| rsi (mean-reversion)       |        2.24 |      0.0134 |     2.48 |      1.63 |     -0.91 |     5.47 |    74.6 | 2.47 |       71 |
| bollinger (mean-reversion) |       13.03 |      0.0745 |     2.94 |      2.99 |     -4.38 |     7.02 |    72.3 | 1.84 |      177 |
| donchian (breakout)        |      -25.22 |     -0.1731 |    -4.63 |     -2.52 |    -25.22 |    -1.87 |     0   | 0    |       17 |
| momentum (momentum)        |      -25.04 |     -0.1728 |    -7.51 |     -7.18 |    -25.04 |    -1.87 |    13.3 | 0.07 |       75 |
| buyhold (benchmark)        |       -2.12 |     -0.0133 |    -1.38 |     -0.18 |     -3.28 |    -1.4  |     0   | 0    |        1 |

### Regime: crash

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|
| sma (trend-following)      |       -3.93 |     -0.0162 |    -0.27 |     -0.3  |    -16.52 |    -0.51 |    29   | 0.89 |       31 |
| rsi (mean-reversion)       |       -0.78 |     -0.0047 |    -0.9  |     -0.46 |     -1.52 |    -1.12 |    65.1 | 0.89 |       63 |
| bollinger (mean-reversion) |      -17.93 |     -0.1175 |    -4.53 |     -4.26 |    -20.98 |    -1.67 |    61.5 | 0.66 |      156 |
| donchian (breakout)        |       12.21 |      0.0808 |     1.12 |      1.29 |    -18.42 |     1.56 |    42.5 | 1.35 |       40 |
| momentum (momentum)        |       -3.44 |     -0.0179 |    -0.51 |     -0.68 |     -8.97 |    -0.82 |    35.8 | 0.96 |       81 |
| buyhold (benchmark)        |       -2.12 |     -0.0166 |    -1.32 |     -0.22 |     -3.77 |    -1.22 |     0   | 0    |        1 |

### Regime: real_BTC

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|
| sma (trend-following)      |      -15.27 |     -0.0436 |    -1.34 |     -1.19 |    -20.72 |    -0.74 |    27.9 | 0.71 |       61 |
| rsi (mean-reversion)       |      -12.39 |     -0.036  |    -3.27 |     -1.46 |    -13.36 |    -0.93 |    55.6 | 0.64 |      153 |
| bollinger (mean-reversion) |      -25.14 |     -0.0783 |    -3.33 |     -1.94 |    -25.15 |    -1    |    60.2 | 0.86 |      231 |
| donchian (breakout)        |      -24.51 |     -0.0739 |    -1.77 |     -1.64 |    -25.28 |    -0.97 |    29   | 0.67 |      100 |
| momentum (momentum)        |       -5.15 |     -0.0085 |    -0.15 |     -0.2  |    -21.44 |    -0.24 |    35.5 | 1.21 |      197 |
| buyhold (benchmark)        |       -2.13 |     -0.0048 |    -1.65 |     -0.13 |     -2.13 |    -1    |     0   | 0    |        1 |

## 2. Walk-forward OUT-OF-SAMPLE on real BTCUSDT 1h

### Out-of-sample (stitched test windows)

| strategy                   |   tot_ret_% |   avg_day_% |   sharpe |   sortino |   maxDD_% |   calmar |   win_% |   PF |   trades |   windows |
|:---------------------------|------------:|------------:|---------:|----------:|----------:|---------:|--------:|-----:|---------:|----------:|
| sma (trend-following)      |      -15.24 |     -0.0597 |    -1.81 |     -1.49 |    -23.21 |    -0.86 |    21.4 | 0.47 |       56 |         9 |
| rsi (mean-reversion)       |       -7.38 |     -0.028  |    -1.98 |     -0.96 |    -10.56 |    -0.93 |    61.3 | 0.84 |      119 |         9 |
| bollinger (mean-reversion) |      -21.39 |     -0.0872 |    -2.87 |     -2.32 |    -21.66 |    -1.28 |    56.3 | 0.81 |      142 |         9 |
| donchian (breakout)        |      -12.41 |     -0.0449 |    -0.99 |     -0.95 |    -32.91 |    -0.5  |    29.9 | 0.86 |       87 |         9 |
| momentum (momentum)        |        1.86 |      0.0101 |     0.24 |      0.24 |    -17.74 |     0.14 |    33.8 | 1.14 |       65 |         9 |
| buyhold (benchmark)        |       -9.97 |     -0.0371 |    -1.47 |     -1.19 |    -11.17 |    -1.19 |     0   | 0    |        7 |         9 |

## 3. Verdict — is 10% average daily return achievable?

**No. Not within several orders of magnitude, and not by any strategy — this is arithmetic, not pessimism.**

A sustained 10% *daily* return compounds to:

- `(1.10)^252  ≈ 26,974,702,268×` starting capital in one trading year
- `(1.10)^365  ≈ 1.283e+15×` over a calendar year
- $100,000 would become **$2.697e+15** in ~12 months

That exceeds the market cap of every crypto asset combined within weeks, so it cannot persist: your own orders would move the market long before then. For reference, the best track records in history (Renaissance Medallion ≈ 66%/yr, Buffett ≈ 20%/yr) correspond to roughly **0.05–0.2% per day**.

### What this framework actually achieves (honest, out-of-sample)

- Best risk-adjusted strategy: **momentum (momentum)**
- OOS average daily return: **0.0101%/day** (≈ +3.7%/yr if it held)
- OOS Sharpe: **0.24**, Sortino: 0.24, max drawdown: -17.7%, Calmar: 0.14
- Profit factor: 1.14, win rate: 33.8%, trades: 65

The 10%/day target is **~993× larger** than the best honest daily return found here.

### Recommendation

Target **risk-adjusted** performance, not a daily percentage. The highest-Sharpe, walk-forward-validated configuration here is **momentum** under the managed risk policy (ATR position sizing, 2% risk/trade, 2.5-ATR stops, 5% daily-loss halt, 25% max-drawdown kill switch). Realistic, sustainable goals for a single-asset system like this are low-single-digit **monthly** returns with a Sharpe above 1 and drawdowns held under ~20%. Push returns higher only via diversification (many uncorrelated symbols/strategies), not leverage on one bet.
