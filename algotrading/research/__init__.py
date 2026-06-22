"""Research layer: backtest orchestration, optimisation, and walk-forward.

These modules sit *on top of* the trading framework and reuse the exact same
engine, portfolio, risk, and execution objects the live system uses. Nothing in
here changes trading logic — it only drives many backtests and aggregates the
results, so any conclusion drawn here transfers directly to live/paper trading.
"""
