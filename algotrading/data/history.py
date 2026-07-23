"""Deterministic historical-OHLCV downloader with strict validation (Step 7).

Generation 1 could not be trusted partly because the bots "did not all share the
same evaluation window". A corrected falsification run needs a REAL, verifiable,
fully-specified price history — not a best-effort scrape. This module fetches
Coinbase's public, key-less candles with:

  * deterministic pagination over an EXPLICIT half-open window [start, end)
    (candles whose open time t satisfies start <= t < end), so the same request
    always issues the same sequence of page requests and yields the same bytes;
  * strict validation that REJECTS duplicate, unordered, misaligned (non-hourly),
    or incomplete data — and NEVER substitutes a synthetic candle for a missing
    real one (missing candles are recorded, not invented);
  * a content hash and a manifest (source, product, timeframe, request time,
    coverage, expected/actual counts, and the exact list of missing candles) so a
    snapshot is auditable and reproducible.

The network call is injected (``fetch=``) so the whole pipeline is unit-tested
offline and deterministically; the default hits Coinbase live.

This is market data only. Downloading it is explicitly authorised (keyless,
public). A snapshot may be labelled ``RECONSTRUCTED`` when it is assembled for a
period that must be flagged as such; a reconstruction must never be used to
repair or re-score Generation 1 evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import pandas as pd

from ..utils.logger import get_logger
from .public import _BASE, _GRANULARITY, PublicMarketData

log = get_logger(__name__)

# Coinbase returns at most this many candles per /candles request.
_MAX_PER_REQUEST = 300

# A raw Coinbase candle row: [time, low, high, open, close, volume], newest-first.
FetchFn = Callable[[str, int, str, str], list]


class HistoryError(ValueError):
    """Raised when fetched history fails an integrity check.

    ``kind`` is a stable machine-readable tag (``duplicate``, ``unordered``,
    ``non_hourly``, ``out_of_range``, ``malformed``, ``incomplete``) so callers
    and tests can assert on the failure mode, not on prose.
    """

    def __init__(self, kind: str, message: str, *, missing: Optional[list] = None):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.missing = missing or []


def _to_epoch(value) -> int:
    """Accept a UTC datetime, an ISO-8601 string, or an epoch int -> epoch secs."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    raise TypeError(f"cannot interpret {value!r} as a time")


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _default_fetch(product: str, gran: int, start_iso: str, end_iso: str) -> list:
    import requests

    resp = requests.get(
        f"{_BASE}/products/{product}/candles",
        params={"granularity": gran, "start": start_iso, "end": end_iso},
        headers={"User-Agent": "algotrading/1.0"},   # Coinbase 403s a blank UA
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


@dataclass
class HistorySnapshot:
    """A validated, hashed historical OHLCV snapshot plus its audit manifest."""

    source: str
    symbol: str
    product: str
    interval: str
    granularity_seconds: int
    start: str                 # ISO, inclusive
    end: str                   # ISO, exclusive
    request_time: str          # ISO, when the download was performed
    rows: List[list]           # ascending [time, low, high, open, close, volume]
    expected_count: int
    actual_count: int
    missing: List[str]         # ISO opens of candles the source did not return
    sha256: str
    label: str = "REAL"        # or "RECONSTRUCTED"

    def manifest(self) -> dict:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "product": self.product,
            "interval": self.interval,
            "granularity_seconds": self.granularity_seconds,
            "start": self.start,
            "end": self.end,
            "request_time": self.request_time,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing_count": len(self.missing),
            "missing": self.missing,
            "coverage_first": _iso(int(self.rows[0][0])) if self.rows else None,
            "coverage_last": _iso(int(self.rows[-1][0])) if self.rows else None,
            "sha256": self.sha256,
            "label": self.label,
        }

    def to_dataframe(self) -> pd.DataFrame:
        if not self.rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            self.rows, columns=["time", "low", "high", "open", "close", "volume"])
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("dt").sort_index()
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    def save(self, directory) -> dict:
        """Write the raw snapshot and its manifest; return the two paths.

        The raw file is the EXACT byte sequence the hash is computed over, so a
        verifier can re-hash the file and compare it to the manifest.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{self.symbol}_{self.interval}_{self.label.lower()}"
        raw_path = directory / f"{stem}.raw.json"
        manifest_path = directory / f"{stem}.manifest.json"
        raw_path.write_text(_canonical(self.rows), encoding="utf-8")
        manifest_path.write_text(
            json.dumps(self.manifest(), indent=2, sort_keys=True), encoding="utf-8")
        return {"raw": str(raw_path), "manifest": str(manifest_path)}


def _canonical(rows: List[list]) -> str:
    """Deterministic serialisation of raw rows -> the bytes we hash."""
    return json.dumps(rows, separators=(",", ":"), sort_keys=True)


def _normalise_row(r) -> list:
    if not isinstance(r, (list, tuple)) or len(r) != 6:
        raise HistoryError("malformed", f"candle row is not 6 fields: {r!r}")
    try:
        t = int(r[0])
        vals = [float(x) for x in r[1:]]
    except (TypeError, ValueError) as exc:
        raise HistoryError("malformed", f"non-numeric candle field in {r!r}") from exc
    return [t, *vals]


def fetch_history(
    symbol: str,
    start,
    end,
    interval: str = "1h",
    *,
    fetch: Optional[FetchFn] = None,
    request_time=None,
    label: str = "REAL",
    require_complete: bool = True,
    source: str = "coinbase-public",
) -> HistorySnapshot:
    """Download and validate candles for ``symbol`` over the half-open [start, end).

    Returns a :class:`HistorySnapshot`. Raises :class:`HistoryError` on any
    integrity failure. With ``require_complete=False`` a snapshot with gaps is
    returned instead of raising, but the gaps are RECORDED in ``missing`` and the
    missing candles are NEVER synthesised.
    """
    gran = _GRANULARITY.get(interval)
    if gran is None:
        raise HistoryError(
            "non_hourly",
            f"interval {interval!r} not supported; use one of {sorted(_GRANULARITY)}")

    start_epoch = _to_epoch(start)
    end_epoch = _to_epoch(end)
    if end_epoch <= start_epoch:
        raise HistoryError("out_of_range", f"end {end!r} must be after start {start!r}")
    if (end_epoch - start_epoch) % gran != 0:
        raise HistoryError(
            "non_hourly",
            f"[start, end) span is not a whole number of {interval} buckets")

    product = PublicMarketData._product(symbol)
    fetch = fetch or _default_fetch
    req_iso = _to_iso_request_time(request_time)

    expected_opens = list(range(start_epoch, end_epoch, gran))
    expected_set = set(expected_opens)

    # ---- deterministic pagination over disjoint 300-bucket windows -----------
    by_time: dict[int, list] = {}
    for i in range(0, len(expected_opens), _MAX_PER_REQUEST):
        chunk = expected_opens[i:i + _MAX_PER_REQUEST]
        w_start, w_end = chunk[0], chunk[-1]
        raw = fetch(product, gran, _iso(w_start), _iso(w_end))
        rows = [_normalise_row(r) for r in raw]

        # Coinbase's contract is newest-first: each response must be strictly
        # descending in time. A non-monotonic response is corrupt -> reject.
        times = [r[0] for r in rows]
        if any(a <= b for a, b in zip(times, times[1:])):
            raise HistoryError(
                "unordered",
                f"response for [{_iso(w_start)}, {_iso(w_end)}] is not "
                "strictly time-descending")

        for r in rows:
            t = r[0]
            if (t - start_epoch) % gran != 0:
                raise HistoryError(
                    "non_hourly", f"candle open {_iso(t)} is not on the {interval} grid")
            if t not in expected_set:
                raise HistoryError(
                    "out_of_range", f"candle open {_iso(t)} is outside [start, end)")
            if t in by_time:
                raise HistoryError("duplicate", f"candle open {_iso(t)} returned twice")
            by_time[t] = r

    present = sorted(by_time)
    ordered_rows = [by_time[t] for t in present]
    missing = [_iso(t) for t in expected_opens if t not in by_time]

    if missing and require_complete:
        raise HistoryError(
            "incomplete",
            f"{len(missing)} of {len(expected_opens)} candles missing; "
            "refusing to substitute synthetic candles",
            missing=missing)

    snapshot = HistorySnapshot(
        source=source,
        symbol=symbol,
        product=product,
        interval=interval,
        granularity_seconds=gran,
        start=_iso(start_epoch),
        end=_iso(end_epoch),
        request_time=req_iso,
        rows=ordered_rows,
        expected_count=len(expected_opens),
        actual_count=len(ordered_rows),
        missing=missing,
        sha256=hashlib.sha256(_canonical(ordered_rows).encode("utf-8")).hexdigest(),
        label=label,
    )
    log.info(
        "history %s %s [%s, %s): %d/%d candles, %d missing, sha256=%s",
        symbol, interval, snapshot.start, snapshot.end,
        snapshot.actual_count, snapshot.expected_count, len(missing),
        snapshot.sha256[:12])
    return snapshot


def _to_iso_request_time(request_time) -> str:
    if request_time is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(request_time, datetime):
        rt = request_time
        if rt.tzinfo is None:
            rt = rt.replace(tzinfo=timezone.utc)
        return rt.isoformat()
    return str(request_time)
