"""Download REAL BTC/ETH 1-hour history from Coinbase's public, key-less API.

This is the operational wrapper around :mod:`algotrading.data.history`. The
library does deterministic pagination, strict validation, hashing, and manifest
generation; this tool adds the two things that belong to an *operation* rather
than a *library*: politeness to a free public endpoint (a small inter-request
delay plus bounded retry/backoff on 429/5xx) and a fixed, reproducible download
window written to a fresh output directory.

Authorised scope (verbatim boundary): "public keyless data downloads" only.
There is no account, no key, and no order placement here — market data only.

Window: an EXPLICIT, fully-closed calendar range, so a re-run downloads the same
candles and produces the same content hash. Ends at 2026-07-01T00:00Z, so the
partial current month is excluded and no candle needs reconstruction; the
snapshot is therefore labelled REAL, not RECONSTRUCTED.

Run:  python -m tools.download_history
It never overwrites Generation 1 evidence or the live state/ directory; it writes
only under historical_data/gen2/.
"""

from __future__ import annotations

import time
from pathlib import Path

from algotrading.data.history import _BASE, _iso, _to_epoch, fetch_history

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "historical_data" / "gen2"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "1h"
START = "2025-07-01T00:00:00+00:00"
END = "2026-07-01T00:00:00+00:00"     # exclusive; fully closed as of 2026-07-23

# Courtesy pacing for the free endpoint (documented public limit ~3 req/s).
_REQUEST_DELAY_S = 0.34
_MAX_RETRIES = 5


def _polite_fetch(product: str, gran: int, start_iso: str, end_iso: str) -> list:
    """Coinbase fetch with a courtesy delay and bounded backoff on 429/5xx."""
    import requests

    url = f"{_BASE}/products/{product}/candles"
    params = {"granularity": gran, "start": start_iso, "end": end_iso}
    headers = {"User-Agent": "algotrading/1.0"}
    delay = 1.0
    for attempt in range(1, _MAX_RETRIES + 1):
        time.sleep(_REQUEST_DELAY_S)
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
            print(f"    {resp.status_code} on {product} attempt {attempt}; "
                  f"backing off {delay:.1f}s")
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"exhausted retries for {product} [{start_iso}, {end_iso}]")


def main() -> int:
    print(f"Downloading {INTERVAL} history [{START}, {END}) -> {OUT_DIR}")
    span_hours = (_to_epoch(END) - _to_epoch(START)) // 3600
    print(f"Window = {span_hours} hourly candles per symbol\n")

    results = []
    for symbol in SYMBOLS:
        print(f"[{symbol}]")
        # require_complete=False so an isolated real gap is RECORDED (never
        # synthesised) instead of aborting the whole download; completeness is
        # reported below so a strict re-fetch can be decided on the evidence.
        snap = fetch_history(
            symbol, START, END, INTERVAL,
            fetch=_polite_fetch, label="REAL", require_complete=False)
        paths = snap.save(OUT_DIR)
        print(f"    candles: {snap.actual_count}/{snap.expected_count}"
              f"  missing: {len(snap.missing)}")
        print(f"    sha256 : {snap.sha256}")
        print(f"    raw    : {paths['raw']}")
        print(f"    manifest: {paths['manifest']}")
        if snap.missing:
            head = ", ".join(snap.missing[:5])
            print(f"    MISSING (recorded, NOT synthesised): {head}"
                  f"{' ...' if len(snap.missing) > 5 else ''}")
        print()
        results.append(snap)

    complete = all(not s.missing for s in results)
    print("COMPLETE (no gaps, strict re-fetch would pass)" if complete
          else "INCOMPLETE (gaps recorded in manifests; no synthetic fill applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
