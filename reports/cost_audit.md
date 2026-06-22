# Gross-vs-Net Cost Audit

- Capital $100; walk-forward train 2160/test 720 bars; real BTCUSDT 1h
- GROSS = all monetary costs zeroed; NET = 0.10%/side fee, 2 bps slippage, next-open fills +1 bp latency, 10%-volume partial fills, $10 min-notional, market impact, 10% APR short-borrow/financing
- Microstructure (fill timing, participation, min-notional, risk policy, params) is identical across both, so the gap is purely trading cost.

## Where each cost is applied
| Cost | Rate set | Applied (equation) | Hits cash | Reported |
|---|---|---|---|---|
| Commission | `simulated.py` ctor / CLI `--commission` | `qty*fill_price*commission_pct` (simulated.py) | `portfolio.update_fill`: `cash -= commission` | `total_commission` |
| Slippage+latency | `slippage_bps`,`latency_bps` | `fill_price = ref*(1±adverse)` (simulated.py) | embedded in `fill_price` debit | `total_slippage` (new) |
| Market impact | `impact_coeff_bps` | `adverse += impact*participation` (simulated.py) | embedded in `fill_price` | in `total_slippage` |
| Financing/borrow | `financing_apr` | `base*apr/periods_per_year` per bar (portfolio._accrue_financing) | `cash -= charge` each bar | `total_financing` (new) |
| Live fees | Binance response | `sum(fills[].commission)` (binance.py) | real exchange | `total_commission` |

## Gross vs net by strategy

| strategy                   |   gross_ret_% |   net_ret_% |   cost_drag_% |   commission_$ |   slippage_$ |   financing_$ |   total_cost_$ |   net_sharpe |
|:---------------------------|--------------:|------------:|--------------:|---------------:|-------------:|--------------:|---------------:|-------------:|
| sma (trend-following)      |        -22.32 |      -32.06 |          9.74 |           7.15 |         2.15 |          0.07 |           9.37 |        -2.67 |
| rsi (mean-reversion)       |         -2.49 |       -8.46 |          5.97 |           4.45 |         1.34 |          0    |           5.79 |        -2.02 |
| bollinger (mean-reversion) |        -14.04 |      -31.5  |         17.47 |          17.96 |         5.39 |          0.76 |          24.11 |        -3.16 |
| donchian (breakout)        |         -9.84 |      -30.78 |         20.94 |          17.97 |         5.39 |          1.65 |          25.01 |        -1.78 |
| momentum (momentum)        |          9.56 |      -16.06 |         25.62 |           7.8  |         2.34 |          0.85 |          10.99 |        -1.45 |
| volatility (volatility)    |         -0.19 |       -8.56 |          8.37 |           9.78 |         2.93 |          0.81 |          13.52 |        -0.76 |
| buyhold (benchmark)        |         -5.38 |       -6.26 |          0.88 |           0.7  |         0.21 |          0    |           0.91 |        -1.23 |
