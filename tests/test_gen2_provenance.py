"""Offline rejection tests for the launch-time market-data provenance gate (Issue 5).

The gate's real job runs against the live Coinbase public endpoint (proven
separately by an on-demand live preflight). Here we prove — deterministically and
without touching the network — that every failure mode the gate must catch DOES
fail closed, and that a clean feed certifies. We drive the row/response validators
through the module's injectable ``http_get`` in ``strict=False`` mode; a single
``strict=True`` test proves launch mode refuses an injected fetch outright (the
"fixture/mock provenance rejection" case).

Fabricated Coinbase candle rows use the real wire shape
``[time, low, high, open, close, volume]`` (seconds, newest-first) so the parser,
grid check, freshness math and plausibility checks all exercise genuine code.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from algotrading.gen2 import provenance as prov  # noqa: E402
from algotrading.gen2.provenance import (  # noqa: E402
    COINBASE_HOST, MIN_WARMUP, REQUIRED_PRODUCTS, ProvenanceError, preflight)

# A plausible ":13-past-the-hour" preflight instant; the newest closed 1h candle
# then closed 13 minutes ago (780s) — comfortably inside the freshness tolerance.
NOW = datetime(2024, 6, 1, 12, 13, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# fake Coinbase transport
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, *, status=200, headers=None, url="", body=None):
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self.content = json.dumps(body).encode("utf-8")

    def json(self):
        return json.loads(self.content)


def _rows(now, *, count=MIN_WARMUP + 5, close_lag_hours=0, price=100.0,
          vol=10.0, off_grid=False, price_override=None, vol_override=None):
    """`count` closed hourly candles, newest-first, ending near `now`.

    ``close_lag_hours`` pushes the whole series back in time (stale feed);
    ``off_grid`` nudges the newest open off the 3600s grid; the ``*_override``
    knobs poison only the newest candle's price/volume.
    """
    now_epoch = int(now.timestamp())
    top_open = (now_epoch // 3600) * 3600 - 3600 - close_lag_hours * 3600
    rows = []
    for k in range(count):
        t = top_open - k * 3600
        if off_grid and k == 0:
            t += 60
        p = price_override if (k == 0 and price_override is not None) else price
        v = vol_override if (k == 0 and vol_override is not None) else vol
        rows.append([t, p * 0.99, p * 1.01, p, p, v])
    return rows


def _http(row_map, *, now=NOW, status=200, url_override=None,
          date="auto", drop_date=False):
    def http_get(url, params=None, headers=None, timeout=None):
        parts = urlsplit(url)
        product = parts.path.split("/")[2]          # /products/<product>/candles
        hdrs = {"Content-Type": "application/json", "Server": "cloudflare"}
        if not drop_date:
            hdrs["Date"] = (formatdate(int(now.timestamp()), usegmt=True)
                            if date == "auto" else date)
        return _FakeResp(status=status, headers=hdrs,
                         url=(url_override or url), body=row_map[product])
    return http_get


def _clean_map(now=NOW, **kw):
    return {p: _rows(now, **kw) for p in REQUIRED_PRODUCTS.values()}


# --------------------------------------------------------------------------
# the clean feed certifies
# --------------------------------------------------------------------------
def test_clean_feed_certifies():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map()))
    assert r.ok and not r.blocked and not r.failed
    assert r.source == "injected"
    # both products fresh; a shared decision boundary is present in each.
    assert r.shared_boundary_epoch_ms is not None
    assert all(p.ok and p.newest_price and p.newest_volume for p in r.products)
    r.assert_ok()                                   # does not raise


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------
def test_stale_candle_rejected():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(close_lag_hours=6)))
    assert not r.ok and r.failed and not r.blocked
    assert any(i.kind == "freshness" for i in r.all_issues())
    with pytest.raises(ProvenanceError):
        r.assert_ok()


def test_stale_server_date_rejected():
    stale = formatdate(int(NOW.timestamp()) - 10 * 3600, usegmt=True)
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(), date=stale))
    # candles are fresh, but the server Date header is not -> freshness issue.
    assert r.failed and any(i.kind == "freshness" for i in r.all_issues())


# --------------------------------------------------------------------------
# product / endpoint identity
# --------------------------------------------------------------------------
def test_wrong_product_rejected():
    wrong = {"BTCUSDT": "BTC-USD", "DOGEUSDT": "DOGE-USD"}
    row_map = {"BTC-USD": _rows(NOW), "DOGE-USD": _rows(NOW)}
    r = preflight(wrong, now=NOW, strict=False, http_get=_http(row_map))
    assert r.failed and any(i.kind == "product" for i in r.all_issues())


def test_wrong_endpoint_host_rejected():
    evil = "https://evil.example.com/products/BTC-USD/candles"
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(), url_override=evil))
    assert r.failed and any(i.kind == "endpoint" for i in r.all_issues())


# --------------------------------------------------------------------------
# grid / coverage / plausibility
# --------------------------------------------------------------------------
def test_off_grid_epoch_rejected():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(off_grid=True)))
    assert r.failed and any(i.kind == "grid" for i in r.all_issues())


def test_insufficient_coverage_rejected():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(count=50)))
    assert r.failed and any(i.kind == "coverage" for i in r.all_issues())


def test_nonpositive_price_rejected():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(price_override=0.0)))
    assert r.failed and any(i.kind == "plausibility" for i in r.all_issues())


def test_nonpositive_volume_rejected():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(vol_override=0.0)))
    assert (r.failed and any(i.kind == "plausibility" and "volume" in i.message
                             for i in r.all_issues()))


# --------------------------------------------------------------------------
# network reachability -> BLOCKED (never launch on uncertain data)
# --------------------------------------------------------------------------
def test_http_error_is_network_blocked():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False,
                  http_get=_http(_clean_map(), status=500))
    assert r.blocked and any(i.kind == "network" for i in r.all_issues())


def test_request_exception_is_network_blocked():
    def boom(url, params=None, headers=None, timeout=None):
        raise ConnectionError("dns failure")
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=False, http_get=boom)
    assert r.blocked and any(i.kind == "network" for i in r.all_issues())


# --------------------------------------------------------------------------
# launch (strict) mode refuses an injected fetch outright (anti-mock)
# --------------------------------------------------------------------------
def test_launch_mode_refuses_injected_fetch():
    r = preflight(REQUIRED_PRODUCTS, now=NOW, strict=True,
                  http_get=_http(_clean_map()))
    assert not r.ok and r.failed
    assert any(i.kind == "mock" for i in r.all_issues())
    # It refused BEFORE making any request: no product rows were validated.
    assert r.products == []


def test_assert_live_fetch_only_refuses_injected_coordinator():
    class _Coord:
        _fetch_ohlcv = staticmethod(lambda *a, **k: None)
    with pytest.raises(ProvenanceError, match="injected fetch"):
        prov.assert_live_fetch_only(_Coord())

    class _Live:
        _fetch_ohlcv = None
    prov.assert_live_fetch_only(_Live())            # genuine feed: allowed
