# Realistic staged plan on a $100 account

- Risk 1%/trade, daily loss cap 1%, daily win-bank 1%, 15% drawdown kill switch
- Full costs: fees, slippage, latency, partial fills, $10 min-notional, 10% APR financing
- Walk-forward OOS, capital compounded forward, real BTCUSDT 1h

| strategy                |   start_$ |   end_$ |   gross_% |   net_% |   maxDD_% |   up_days |   down_days |   best_day_$ |   worst_day_$ |   sharpe |
|:------------------------|----------:|--------:|----------:|--------:|----------:|----------:|------------:|-------------:|--------------:|---------:|
| momentum (momentum)     |       100 |  104.37 |       7.1 |     4.4 |      -3.5 |        54 |          59 |         1.91 |         -1.27 |     1.14 |
| volatility (volatility) |       100 |   93.06 |      -0.2 |    -6.9 |     -12.5 |        45 |          70 |         2.28 |         -1.59 |    -1.46 |
| rsi (mean-reversion)    |       100 |   97.26 |       0.5 |    -2.7 |      -5.5 |        33 |          34 |         2.11 |         -1.25 |    -1.15 |
| donchian (breakout)     |       100 |   86.36 |       0.2 |   -13.6 |     -21   |        53 |          88 |         3.08 |         -1.97 |    -1.69 |
| buyhold (benchmark)     |       100 |   94.92 |      -4.7 |    -5.1 |      -5.7 |        56 |          60 |         0.91 |         -1.04 |    -1.48 |
