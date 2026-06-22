"""Canonical strategy registry and parameter grids, shared by the CLI and the
research scripts so there is a single source of truth for "which strategies
exist and what is a sensible search space for each".

Grids are deliberately small and coarse. Walk-forward re-optimises on every
window, so a fine grid mostly adds runtime and overfitting surface, not insight.
"""

from __future__ import annotations

from typing import Dict, Type

from ..strategy.base import Strategy
from ..strategy.bollinger import BollingerReversionStrategy
from ..strategy.buy_and_hold import BuyAndHoldStrategy
from ..strategy.donchian_breakout import DonchianBreakoutStrategy
from ..strategy.momentum import MomentumStrategy
from ..strategy.rsi import RSIStrategy
from ..strategy.sma_crossover import SMACrossoverStrategy
from ..strategy.volatility_breakout import VolatilityBreakoutStrategy

STRATEGY_REGISTRY: Dict[str, Type[Strategy]] = {
    "sma": SMACrossoverStrategy,
    "rsi": RSIStrategy,
    "bollinger": BollingerReversionStrategy,
    "donchian": DonchianBreakoutStrategy,
    "momentum": MomentumStrategy,
    "volatility": VolatilityBreakoutStrategy,
    "buyhold": BuyAndHoldStrategy,
}

# Strategy family label for reporting.
FAMILY = {
    "sma": "trend-following",
    "rsi": "mean-reversion",
    "bollinger": "mean-reversion",
    "donchian": "breakout",
    "momentum": "momentum",
    "volatility": "volatility",
    "buyhold": "benchmark",
}

# Parameter search grids for optimisation / walk-forward.
PARAM_GRIDS: Dict[str, dict] = {
    "sma": {"fast": [10, 20, 30], "slow": [50, 100, 150]},
    "rsi": {"period": [10, 14, 21], "oversold": [25, 30, 35], "exit_level": [50, 55]},
    "bollinger": {"window": [20, 40], "entry_z": [1.5, 2.0, 2.5], "exit_z": [0.0, 0.5]},
    "donchian": {"entry": [20, 40, 55], "exit": [10, 20], "trend": [0, 100]},
    # Longer lookbacks + a hysteresis exit_band (hold through small reversals)
    # cut turnover sharply, which matters most on a small account where fees and
    # the $10 min-notional bite hardest. Short/low-threshold combos are kept OUT
    # on purpose: the optimiser picks them in-sample then they fail out-of-sample
    # (over-trading). Validated OOS: ~33 trades, Sharpe ~1.14. See reports/fee_cut.md.
    "momentum": {"lookback": [96, 168], "threshold": [1.0, 1.5],
                 "exit_band": [0.0, -0.3]},
    "volatility": {"window": [20, 40], "kc_mult": [1.5, 2.0], "squeeze_lookback": [6, 12]},
    "buyhold": {},
}

# Reasonable single-shot defaults (used for baseline runs without optimisation).
DEFAULT_PARAMS: Dict[str, dict] = {
    "sma": {"fast": 20, "slow": 100},
    "rsi": {"period": 14, "oversold": 30, "exit_level": 50},
    "bollinger": {"window": 20, "entry_z": 2.0, "exit_z": 0.5},
    "donchian": {"entry": 55, "exit": 20, "trend": 100},
    "momentum": {"lookback": 96, "threshold": 1.0},  # fee-efficient: ~3x fewer trades
    "volatility": {"window": 20, "kc_mult": 1.5, "squeeze_lookback": 6},
    "buyhold": {},
}
