"""YAML config loading with environment-variable fallback for secrets.

Precedence for API keys: explicit config file value > environment variable.
Never commit real keys; config.yaml is gitignored and config.example.yaml ships
with placeholders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict

import yaml


@dataclass
class AppConfig:
    exchange: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    initial_capital: float = 100_000.0
    cache_dir: str = "data_cache"
    log_level: str = "INFO"
    log_file: str = ""           # optional: also write logs here (CLI --log-file overrides)
    financing_apr: float = 0.0   # yearly borrow/funding rate on short/leveraged positions
    risk: Dict[str, Any] = field(default_factory=dict)


def load_config(path: str | None = None) -> AppConfig:
    data: Dict[str, Any] = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    return AppConfig(
        exchange=data.get("exchange", "binance"),
        api_key=data.get("api_key") or os.getenv("BINANCE_API_KEY", ""),
        api_secret=data.get("api_secret") or os.getenv("BINANCE_API_SECRET", ""),
        testnet=data.get("testnet", True),
        initial_capital=float(data.get("initial_capital", 100_000.0)),
        cache_dir=data.get("cache_dir", "data_cache"),
        log_level=data.get("log_level", "INFO"),
        log_file=data.get("log_file", "") or "",
        financing_apr=float(data.get("financing_apr", 0.0)),
        risk=data.get("risk", {}),
    )
