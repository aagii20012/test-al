"""Offline tests for the historical-data downloader (Step 7, Decision 5).

The network is injected, so these run deterministically with no internet. The
"source" dict below is a stand-in for what Coinbase's API would return — it is a
TEST DOUBLE of the exchange, not fabricated market data smuggled into a run (the
prohibition in Decision 5 is on the downloader inventing candles to paper over
gaps, which is exactly what these tests prove it never does).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from algotrading.data import history
from algotrading.data.history import (
    HistoryError,
    _canonical,
    _to_epoch,
    fetch_history,
)

GRAN = 3600
BASE = 1_704_067_200          # 2024-01-01T00:00:00Z, exactly on the hour grid


def _source(n, *, gran=GRAN, base=BASE, drop=(), extra=None):
    """Ascending {epoch: candle_row} the fake exchange will serve.

    ``drop`` removes candles (simulates a gap); ``extra`` injects an off-grid or
    out-of-range candle (simulates a corrupt source).
    """
    src = {}
    price = 100.0
    for k in range(n):
        t = base + k * gran
        if t in drop:
            price *= 1.001
            continue
        o, c = price, price * 1.001
        src[t] = [t, o * 0.998, c * 1.002, o, c, 1000.0 + k]
        price = c
    if extra is not None:
        t, row = extra
        src[t] = row
    return src


def _make_fetch(src, *, calls=None, dup_from_prev=False):
    """A fake Coinbase fetch: returns in-window candles newest-first.

    ``dup_from_prev`` makes each window (after the first) also emit the last
    candle of the previous window — a cross-window duplicate.
    """
    prev_last = {"t": None}

    def fetch(product, gran, start_iso, end_iso):
        if calls is not None:
            calls.append((product, gran, start_iso, end_iso))
        s, e = _to_epoch(start_iso), _to_epoch(end_iso)
        window = [src[t] for t in sorted(src) if s <= t <= e]
        desc = list(reversed(window))                       # newest-first
        if dup_from_prev and prev_last["t"] is not None and prev_last["t"] < s:
            desc = desc + [src[prev_last["t"]]]             # still descending
        if window:
            prev_last["t"] = window[-1][0]
        return desc

    return fetch


def test_happy_path_paginates_hashes_and_is_deterministic():
    src = _source(350)
    calls_a, calls_b = [], []
    snap_a = fetch_history("BTCUSDT", BASE, BASE + 350 * GRAN,
                           fetch=_make_fetch(src, calls=calls_a),
                           request_time="2024-01-15T00:00:00+00:00")
    snap_b = fetch_history("BTCUSDT", BASE, BASE + 350 * GRAN,
                           fetch=_make_fetch(src, calls=calls_b),
                           request_time="2024-01-15T00:00:00+00:00")

    assert snap_a.expected_count == 350
    assert snap_a.actual_count == 350
    assert snap_a.missing == []
    # 350 candles over 300-per-request pages -> exactly two page requests.
    assert len(calls_a) == 2
    # Same inputs -> identical page requests and identical content hash.
    assert calls_a == calls_b
    assert snap_a.sha256 == snap_b.sha256
    assert len(snap_a.sha256) == 64
    # Rows are ascending and cover the whole window.
    times = [r[0] for r in snap_a.rows]
    assert times == sorted(times)
    assert times[0] == BASE and times[-1] == BASE + 349 * GRAN
    assert snap_a.to_dataframe().shape == (350, 5)
    m = snap_a.manifest()
    assert m["product"] == "BTC-USD"
    assert m["missing_count"] == 0
    assert m["interval"] == "1h" and m["granularity_seconds"] == 3600


def test_duplicate_across_windows_is_rejected(monkeypatch):
    monkeypatch.setattr(history, "_MAX_PER_REQUEST", 3)
    src = _source(7)
    with pytest.raises(HistoryError) as exc:
        fetch_history("BTCUSDT", BASE, BASE + 7 * GRAN,
                      fetch=_make_fetch(src, dup_from_prev=True))
    assert exc.value.kind == "duplicate"


def test_unordered_response_is_rejected():
    src = _source(5)

    def ascending_fetch(product, gran, start_iso, end_iso):
        s, e = _to_epoch(start_iso), _to_epoch(end_iso)
        return [src[t] for t in sorted(src) if s <= t <= e]  # WRONG: ascending

    with pytest.raises(HistoryError) as exc:
        fetch_history("BTCUSDT", BASE, BASE + 5 * GRAN, fetch=ascending_fetch)
    assert exc.value.kind == "unordered"


def test_non_hourly_candle_is_rejected():
    off_grid = BASE + 1800                                   # 30 min past the hour
    src = _source(5, extra=(off_grid, [off_grid, 1, 2, 1, 2, 3.0]))
    with pytest.raises(HistoryError) as exc:
        fetch_history("BTCUSDT", BASE, BASE + 5 * GRAN, fetch=_make_fetch(src))
    assert exc.value.kind == "non_hourly"


def test_missing_candle_is_recorded_never_synthesised():
    gap = BASE + 2 * GRAN
    src = _source(6, drop=(gap,))

    # Strict mode refuses and names the gap.
    with pytest.raises(HistoryError) as exc:
        fetch_history("BTCUSDT", BASE, BASE + 6 * GRAN, fetch=_make_fetch(src))
    assert exc.value.kind == "incomplete"
    assert history._iso(gap) in exc.value.missing

    # Lenient mode returns the snapshot, records the gap, and does NOT invent a
    # candle to fill it.
    snap = fetch_history("BTCUSDT", BASE, BASE + 6 * GRAN,
                         fetch=_make_fetch(src), require_complete=False)
    assert snap.expected_count == 6
    assert snap.actual_count == 5
    assert snap.missing == [history._iso(gap)]
    assert gap not in [r[0] for r in snap.rows]             # no synthetic fill


def test_span_and_range_validation():
    with pytest.raises(HistoryError) as e1:
        fetch_history("BTCUSDT", BASE, BASE, fetch=_make_fetch(_source(1)))
    assert e1.value.kind == "out_of_range"

    with pytest.raises(HistoryError) as e2:                 # span not whole hours
        fetch_history("BTCUSDT", BASE, BASE + GRAN + 60, fetch=_make_fetch(_source(2)))
    assert e2.value.kind == "non_hourly"


def test_save_writes_raw_and_manifest_with_matching_hash(tmp_path):
    src = _source(10)
    snap = fetch_history("BTCUSDT", BASE, BASE + 10 * GRAN,
                         fetch=_make_fetch(src),
                         request_time="2024-01-15T00:00:00+00:00")
    paths = snap.save(tmp_path)

    raw_bytes = open(paths["raw"], encoding="utf-8").read()
    assert hashlib.sha256(raw_bytes.encode("utf-8")).hexdigest() == snap.sha256
    assert raw_bytes == _canonical(snap.rows)

    manifest = json.loads(open(paths["manifest"], encoding="utf-8").read())
    assert manifest["sha256"] == snap.sha256
    assert manifest["actual_count"] == 10
    assert manifest["missing_count"] == 0
    assert manifest["label"] == "REAL"


def test_reconstructed_label_flows_through(tmp_path):
    src = _source(5)
    snap = fetch_history("ETHUSDT", BASE, BASE + 5 * GRAN,
                         fetch=_make_fetch(src), label="RECONSTRUCTED")
    assert snap.label == "RECONSTRUCTED"
    assert snap.manifest()["label"] == "RECONSTRUCTED"
    paths = snap.save(tmp_path)
    assert "reconstructed" in paths["raw"]
