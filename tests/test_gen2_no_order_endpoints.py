"""Gen2 must be structurally incapable of placing an order.

Two independent guarantees:

  1. STATIC — every ``algotrading/gen2/*.py`` file is parsed with ``ast`` (so
     comments / docstrings that merely *mention* live trading do not trip the
     scan) and must not import a live-order module (``algotrading.execution.live``,
     anything under ``algotrading.exchange``, or the ``binance`` client) nor
     reference an order-placing symbol (``place_market_order`` & friends).

  2. DYNAMIC — a fresh interpreter imports every gen2 module and then asserts the
     live-order modules are absent from ``sys.modules``. This catches a
     *transitive* import that a text scan would miss.

Gen2 imports only market-data readers + the SIMULATED execution handler. There
is no account, no credential, no order endpoint anywhere in its import graph.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN2_DIR = os.path.join(REPO_ROOT, "algotrading", "gen2")
GEN2_PKG_PARTS = ["algotrading", "gen2"]

# Fully-resolved module names (or prefixes) gen2 may never import.
FORBIDDEN_MODULE_PREFIXES = (
    "algotrading.execution.live",   # LiveExecutionHandler -> real orders
    "algotrading.exchange",         # any venue adapter (binance, base, ...)
    "binance",                      # the python-binance order client
)
# Order-placing / account identifiers that must not appear anywhere in gen2.
FORBIDDEN_NAMES = frozenset({
    "place_market_order", "place_order", "create_order", "cancel_order",
    "account_balances", "LiveExecutionHandler", "BinanceExchange",
})


def _gen2_files():
    return sorted(
        os.path.join(GEN2_DIR, f)
        for f in os.listdir(GEN2_DIR)
        if f.endswith(".py"))


def _resolve_import_from(node: ast.ImportFrom) -> str:
    """Resolve a ``from .. import x`` node to an absolute module path.

    A module file's package is its containing package (``algotrading.gen2``);
    ``level`` steps up from there (1 = same package, 2 = parent, ...).
    """
    if node.level == 0:
        return node.module or ""
    keep = len(GEN2_PKG_PARTS) - (node.level - 1)
    base = ".".join(GEN2_PKG_PARTS[:max(0, keep)])
    if base and node.module:
        return f"{base}.{node.module}"
    return base or (node.module or "")


def _is_forbidden_module(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)


@pytest.mark.parametrize("path", _gen2_files(), ids=os.path.basename)
def test_no_forbidden_imports_or_symbols(path):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    bad_modules = []
    bad_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    bad_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = _resolve_import_from(node)
            if _is_forbidden_module(mod):
                bad_modules.append(mod)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES:
                bad_names.append(node.attr)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                bad_names.append(node.id)

    assert not bad_modules, f"{os.path.basename(path)} imports live-order module(s): {bad_modules}"
    assert not bad_names, f"{os.path.basename(path)} references order symbol(s): {bad_names}"


def test_importing_gen2_never_loads_live_order_modules():
    """A fresh interpreter importing every gen2 module must not pull a live-order
    module into ``sys.modules`` (catches transitive imports)."""
    script = (
        "import importlib, sys\n"
        "mods = ['algotrading.gen2', 'algotrading.gen2.experiment',\n"
        "        'algotrading.gen2.snapshot', 'algotrading.gen2.coordinator',\n"
        "        'algotrading.gen2.dashboard']\n"
        "for m in mods:\n"
        "    importlib.import_module(m)\n"
        "forbidden = ['algotrading.execution.live', 'algotrading.exchange.binance',\n"
        "             'binance']\n"
        "present = [m for m in forbidden if m in sys.modules]\n"
        "sys.stdout.write('PRESENT:' + ','.join(present) if present else 'CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"import failed: {proc.stderr}"
    assert proc.stdout.strip() == "CLEAN", (
        f"gen2 import graph pulled in a live-order module: {proc.stdout} {proc.stderr}")
