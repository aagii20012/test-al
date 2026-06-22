# algotrading

A production-style, event-driven algorithmic trading framework in Python with
**identical logic for backtesting and live trading**.

The same `Strategy`, `Portfolio`, and `RiskManager` objects run in both modes.
Only two components are swapped:

| Component         | Backtest                  | Live                          |
|-------------------|---------------------------|-------------------------------|
| `DataHandler`     | `HistoricCSVDataHandler`  | `LiveDataHandler` (Binance)   |
| `ExecutionHandler`| `SimulatedExecutionHandler` | `LiveExecutionHandler` (Binance) |

This guarantees that what you backtest is what you trade.

---

## Architecture

Event-driven pipeline. Everything communicates through a single in-memory event
queue, so the control flow is identical in both modes:

```
            ┌──────────────┐
            │  DataHandler │  emits  MarketEvent
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │   Strategy   │  emits  SignalEvent
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  Portfolio   │  consults RiskManager → emits OrderEvent
            │  + Risk      │
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  Execution   │  emits  FillEvent
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  Portfolio   │  updates positions / equity on FillEvent
            └──────────────┘
```

Event types (`algotrading/core/events.py`):

- **MarketEvent** – new bar(s) available.
- **SignalEvent** – a strategy's directional view (`LONG` / `SHORT` / `EXIT`).
- **OrderEvent** – a sized, risk-checked order to send to an exchange.
- **FillEvent** – a confirmed execution with price, quantity, commission.

The event *queue* is the single source of truth. The `BacktestEngine` and
`LiveEngine` both run the exact same dispatch loop
(`algotrading/engine/loop.py`); the only difference is the clock that drives it.

---

## Folder structure

```
algo/
├── README.md
├── requirements.txt
├── config/
│   └── config.example.yaml        # copy to config.yaml and fill in keys
├── algotrading/
│   ├── core/
│   │   ├── enums.py               # Direction, SignalType, OrderType, ...
│   │   ├── events.py              # Market/Signal/Order/Fill events
│   │   └── event_queue.py         # thin wrapper over queue.Queue
│   ├── data/
│   │   ├── base.py                # DataHandler ABC
│   │   ├── historical.py          # CSV + synthetic + Binance REST download
│   │   └── live.py                # LiveDataHandler (polls Binance klines)
│   ├── strategy/
│   │   ├── base.py                # Strategy ABC (plug-and-play)
│   │   ├── sma_crossover.py
│   │   ├── rsi.py
│   │   └── buy_and_hold.py
│   ├── portfolio/
│   │   └── portfolio.py           # positions, holdings, equity curve
│   ├── risk/
│   │   └── risk_manager.py        # sizing, stop-loss, leverage, VaR
│   ├── execution/
│   │   ├── base.py                # ExecutionHandler ABC
│   │   ├── simulated.py           # slippage + commission model
│   │   └── live.py                # Binance order placement
│   ├── exchange/
│   │   ├── base.py                # Exchange ABC
│   │   └── binance.py             # python-binance wrapper (live + REST)
│   ├── analytics/
│   │   ├── performance.py         # Sharpe, Sortino, drawdown, CAGR, ...
│   │   └── dashboard.py           # standalone HTML report (inline-SVG charts)
│   ├── engine/
│   │   ├── loop.py                # shared event-dispatch loop
│   │   ├── backtest.py
│   │   └── live.py
│   ├── utils/
│   │   ├── config.py
│   │   └── logger.py
│   └── cli.py                     # `python -m algotrading.cli ...`
├── examples/
│   ├── run_backtest.py
│   └── run_live.py
└── tests/
    ├── test_events.py
    ├── test_portfolio.py
    ├── test_risk.py
    └── test_backtest.py
```

---

## Quick start

```bash
pip install -r requirements.txt

# 1. Backtest on synthetic data — no API keys, no network required:
python -m algotrading.cli backtest --strategy sma --symbols BTCUSDT --synthetic

# 2. Backtest on real Binance history (downloads & caches to data_cache/):
python -m algotrading.cli download --symbols BTCUSDT --interval 1h --days 365
python -m algotrading.cli backtest --strategy sma --symbols BTCUSDT --interval 1h

# Produce a standalone HTML dashboard (equity curve, drawdown, trades, metrics)
# and open it in your browser — no server, no external assets, works offline:
python -m algotrading.cli backtest --strategy sma --symbols BTCUSDT --synthetic \
    --dashboard report.html --open

# 3. Paper/live trading (testnet recommended first):
cp config/config.example.yaml config/config.yaml   # add your keys
python -m algotrading.cli live --strategy sma --symbols BTCUSDT --interval 1m
```

Run the test suite:

```bash
pytest -q
```

---

## Design decisions

**Why event-driven instead of vectorized?**
Vectorized backtests (operate on a whole DataFrame at once) are fast but cannot
faithfully model intrabar ordering, partial fills, latency, or stateful risk
rules — so they systematically *overstate* performance via look-ahead bias. An
event loop processes one bar at a time, exactly as a live system receives one
tick at a time. The cost is speed; the benefit is that backtest and live code
are literally the same objects.

**Why a single shared dispatch loop?**
`engine/loop.py` contains the one canonical state machine. Both engines call it.
This is the structural guarantee against "it worked in backtest but behaved
differently live" — there is no second implementation to drift.

**Why is risk a separate layer from the portfolio?**
The `Portfolio` answers *"what do I own and what is it worth?"*. The
`RiskManager` answers *"am I allowed to place this order, and how big?"*. Keeping
them separate means you can swap risk policies (fixed-fractional, volatility
targeting, Kelly) without touching accounting, and unit-test each in isolation.

**Why abstract the exchange behind an interface?**
`exchange/base.py` defines the contract; `binance.py` implements it. Adding
Alpaca or Coinbase is a new file, not a refactor. The execution handlers depend
on the abstraction, not on `python-binance`.

**Slippage & commission are first-class in the simulator.**
`SimulatedExecutionHandler` applies a configurable bps slippage and a maker/taker
commission so backtest fills are pessimistic by default. Optimistic fills are the
most common cause of strategies that "work" only on paper.

**Decimal-free, float-based pricing.**
For research-grade backtesting floats are fine and fast. For real money on spot,
the live execution layer rounds order quantities to the exchange's `stepSize`
filters before submission (see `exchange/binance.py`).

---

## Extending: write your own strategy

Subclass `Strategy` and implement `calculate_signals`. Drop the file in
`algotrading/strategy/` and register it in `cli.py`'s `STRATEGIES` map.

```python
from algotrading.strategy.base import Strategy
from algotrading.core.events import SignalEvent
from algotrading.core.enums import SignalType

class MyStrategy(Strategy):
    def calculate_signals(self, event):
        for symbol in self.symbols:
            bars = self.data.get_latest_bars(symbol, n=20)
            if len(bars) < 20:
                continue
            # ... your logic ...
            self.events.put(SignalEvent(symbol, bars[-1].dt, SignalType.LONG))
```

That's the entire contract. The portfolio, risk, execution, and analytics layers
require no changes.

---

## Safety notes

- Always validate on **Binance testnet** (`testnet: true` in config) before
  committing real funds.
- The live engine is conservative: it refuses to start if `max_leverage`,
  `max_position_pct`, or stop-loss settings are missing from config.
- This is a framework and educational reference, **not** financial advice. Trade
  at your own risk.
