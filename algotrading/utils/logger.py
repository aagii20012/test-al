"""Centralized logging.

A single configured root so every module's `get_logger(__name__)` shares format
and level. Call `configure_logging()` once at program start (the CLI does this).
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO", logfile: str | None = None) -> None:
    global _CONFIGURED
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        # Create the parent folder if needed, append so restarts extend the same
        # record, and force UTF-8 so non-Latin content (e.g. CJK coin names) never
        # crashes the file write on Windows.
        parent = os.path.dirname(os.path.abspath(logfile))
        os.makedirs(parent, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        # Lazy default so library use without explicit setup still logs sanely.
        configure_logging()
    return logging.getLogger(name)
