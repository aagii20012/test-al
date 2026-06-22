# $100 Account · $50/day Feasibility Study

- Starting capital: **$100**; aspirational target: **$50/day** (= 50% of capital per day)
- Costs: 0.10%/side fee, 2.0 bps slippage; realism: next-bar-open fills (latency), 10%-of-volume partial fills, $10 min-notional
- Risk: ATR sizing @ 2% risk/trade, 2.5-ATR stop, 6% take-profit, 5% daily-loss halt, 25% max-drawdown suspension
- Walk-forward: train 2160 / test 720 bars, re-optimised by Sharpe each window; data = real BTCUSDT 1h

## Strategy comparison


### Walk-forward OUT-OF-SAMPLE · $100 account · real BTCUSDT 1h

| strategy                   |   net_$ |   ROC_% |   $/day |   sharpe |   maxDD_% |   win_% |   PF |   trades |
|:---------------------------|--------:|--------:|--------:|---------:|----------:|--------:|-----:|---------:|
| sma (trend-following)      |  -32.04 |  -32.04 | -0.1187 |    -2.66 |    -42.21 |    15.9 | 0.38 |       44 |
| rsi (mean-reversion)       |   -8.46 |   -8.46 | -0.0313 |    -2.02 |    -11.23 |    55.6 | 0.74 |       63 |
| bollinger (mean-reversion) |  -30.91 |  -30.91 | -0.1145 |    -3.08 |    -31.23 |    57.8 | 0.82 |      147 |
| donchian (breakout)        |  -32.72 |  -32.72 | -0.1212 |    -1.95 |    -48.29 |    30.3 | 0.79 |       99 |
| momentum (momentum)        |  -15.31 |  -15.31 | -0.0567 |    -1.37 |    -22.48 |    30.8 | 0.71 |       65 |
| volatility (volatility)    |   -7.83 |   -7.83 | -0.029  |    -0.68 |    -20.9  |    33.7 | 1.06 |       83 |
| buyhold (benchmark)        |   -6.26 |   -6.26 | -0.0224 |    -1.23 |     -7.73 |    33.3 | 0.56 |        9 |

## Verdict — $50/day on a $100 account?

**No. $50/day on $100 is a 50% **daily** return — impossible to sustain, and self-contradictory with the risk rules in the brief.**

Two independent reasons:

**1. Compounding math.** 50%/day compounds to `(1.5)^252 ≈ 1e44×` capital in a trading year — more money than exists on Earth within weeks. Even held flat (withdrawing $50/day), you must earn 50% of the account every day with perfect consistency.

**2. It contradicts '1–2% risk per trade'.** Risking 2% of $100 is **$2 per trade**. To net **$50** you would need a **+25R** outcome *every day* — a 25-to-1 reward on risk, won daily. Real edges run well under 1R expectancy. You cannot simultaneously cap risk at $2 and target $50; the two requirements are mutually exclusive.

### What the data actually shows (out-of-sample, $100, full realism)

- Best risk-adjusted strategy: **volatility (volatility)**
- Net profit over the OOS year: **$-7.83** (ROC -7.83%)
- Average daily profit: **$-0.0290/day** vs the **$50/day** target
- Sharpe -0.68, max drawdown -20.9%, profit factor 1.06, 83 trades

The $50/day goal is on the order of **1,724×** the magnitude of the best daily result the engine produced on $100 — and that result was negative.

### Highest sustainable target instead

On a single asset, after costs, a defensible OOS goal is roughly **0.05–0.2% of equity per day** (~20–100%/yr) at Sharpe > 1 with drawdowns under ~20% — and only the best strategy here even approached the low end out-of-sample. On **$100**, that is cents to a few tens of cents per day. The honest expected range for a disciplined $100 bot is roughly **−$0.20 to +$0.20 per day**; treat anything above that as luck, not edge.


### Capital required to reasonably target $50/day

Assuming a genuinely sustainable daily return (net of costs), the capital needed so that $50 is that return:

| Sustainable daily return | Implied annual* | Capital for $50/day |
|---|---|---|
| 0.05%/day (excellent, rare) | 20%/yr | $100,000 |
| 0.10%/day (excellent, rare) | 44%/yr | $50,000 |
| 0.20%/day (exceptional) | 107%/yr | $25,000 |
| 0.30%/day (exceptional) | 198%/yr | $16,667 |
| 0.50%/day (almost certainly unsustainable) | 517%/yr | $10,000 |

*Compounded; shown to convey how extreme even 'modest' daily rates are. A 0.1%/day edge (~44%/yr) is already world-class.

This study's best out-of-sample result was **not reliably positive** (net $-7.83, Sharpe -0.68), so no account size turns it into $50/day: more capital scales a non-edge into a bigger loss, not a profit. The capital figures above presuppose a genuine positive edge this single-asset bot did not demonstrate.


### Recommendation

1. **Keep the $100 on Binance _testnet_** and run **volatility** under the strict risk policy to validate execution and behaviour — not to get rich.
2. **Target risk-adjusted return** (Sharpe/Calmar), capped at ~1–2% risk/trade, with a realistic monthly goal in the low single digits of percent.
3. **Grow via capital and diversification** across many uncorrelated symbols/strategies — never leverage on one bet. $50/day becomes *reasonable* near the capital levels in the table above, not at $100.
