"""Launch-time market-data provenance gate for Generation 2.

Before an operator activates a Generation-2 experiment, this gate proves that the
market data a live tick will consume is genuinely the intended Coinbase public
feed and genuinely fresh — not a fixture, monkeypatch, local cache, or injected
mock. It performs the keyless public preflight ITSELF (a direct HTTPS request it
controls), never through the coordinator's injectable ``fetch_ohlcv``, precisely
so that whatever a test or a rogue caller wired into the coordinator cannot spoof
the proof.

What it verifies for EACH required product, all fail-closed:

  * endpoint identity — the request resolved to the intended Coinbase public host
    ``api.exchange.coinbase.com`` over HTTPS at the ``/products/<id>/candles`` path
    (the response's own final URL is checked, not just the URL we asked for);
  * product identity — the products are exactly BTC-USD and ETH-USD;
  * grid — every candle open time is a genuine UTC hourly boundary
    (``epoch % 3600 == 0``), strictly increasing and unique;
  * freshness — the newest CLOSED candle closed within an explicit tolerance of the
    current UTC time, AND the server's own HTTP ``Date`` header is within that
    tolerance (a stale local cache carries no fresh server Date);
  * coverage — at least ``min_warmup`` closed candles exist up to the shared
    decision boundary present in EVERY product;
  * plausibility — the newest closed candle reports a positive, finite price and a
    positive, finite volume, with sane OHLC ordering — WITHOUT hard-coding an
    expected price band (structure + freshness are the primary test, not "is BTC
    near $X");
  * anti-mock — in launch (strict) mode the request must be the gate's own genuine
    ``requests.get`` (not an injected callable), the ``requests`` library must be
    the real one, and ``PublicMarketData.fetch_ohlcv`` must be un-monkeypatched.

The raw response bytes + response metadata are hashed (SHA-256) and preserved in
the report so the exact market bytes the decision rested on are auditable after
the fact.

Network policy: if the endpoint cannot be reached, the gate records a ``network``
issue and the caller must STOP with BLOCKED rather than launch on uncertain data
— it never falls back to a cache.

This module imports only stdlib + (lazily) ``requests`` + the hashing helper. It
never imports an exchange adapter, a live-execution handler, or any order
endpoint (see the no-order-endpoints test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

from .experiment import sha256_bytes

# The intended public endpoint (keyless, read-only market data).
COINBASE_HOST = "api.exchange.coinbase.com"
COINBASE_SCHEME = "https"
COINBASE_BASE = f"{COINBASE_SCHEME}://{COINBASE_HOST}"
CANDLES_PATH_TEMPLATE = "/products/{product}/candles"

# The exact products a Generation-2 experiment is allowed to trade.
REQUIRED_PRODUCTS: Dict[str, str] = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD"}

_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                "6h": 21600, "1d": 86400}

# Newest closed candle (and the server's Date header) must be at most this old.
# Generous enough that a legitimately-timed :13/:43 preflight never fails
# spuriously, tight enough that an hours/days-old cache is caught.
DEFAULT_FRESHNESS_TOLERANCE_S = 2 * 3600

MIN_WARMUP = 110


class ProvenanceError(RuntimeError):
    """The market-data provenance gate refused to certify the feed."""


@dataclass
class ProvenanceIssue:
    kind: str        # network | endpoint | product | grid | freshness |
                     # coverage | plausibility | mock
    message: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


@dataclass
class ProductProvenance:
    symbol: str
    product: str
    url: str
    ok: bool = False
    http_status: Optional[int] = None
    server_date_utc: Optional[str] = None
    server_header: Optional[str] = None
    content_type: Optional[str] = None
    raw_sha256: Optional[str] = None
    raw_bytes: Optional[int] = None
    candle_count: int = 0
    newest_open_epoch_ms: Optional[int] = None
    newest_close_epoch_ms: Optional[int] = None
    newest_close_age_s: Optional[float] = None
    newest_price: Optional[float] = None
    newest_volume: Optional[float] = None
    issues: List[ProvenanceIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "product": self.product,
            "url": self.url,
            "ok": self.ok,
            "http_status": self.http_status,
            "server_date_utc": self.server_date_utc,
            "server_header": self.server_header,
            "content_type": self.content_type,
            "raw_sha256": self.raw_sha256,
            "raw_bytes": self.raw_bytes,
            "candle_count": self.candle_count,
            "newest_open_epoch_ms": self.newest_open_epoch_ms,
            "newest_close_epoch_ms": self.newest_close_epoch_ms,
            "newest_close_age_s": self.newest_close_age_s,
            "newest_price": self.newest_price,
            "newest_volume": self.newest_volume,
            "issues": [str(i) for i in self.issues],
        }


@dataclass
class ProvenanceReport:
    checked_utc: str
    interval: str
    freshness_tolerance_s: int
    min_warmup: int
    strict: bool
    source: str                                   # network | injected
    products: List[ProductProvenance] = field(default_factory=list)
    shared_boundary_epoch_ms: Optional[int] = None
    issues: List[ProvenanceIssue] = field(default_factory=list)   # cross-product

    def all_issues(self) -> List[ProvenanceIssue]:
        out = list(self.issues)
        for p in self.products:
            out.extend(p.issues)
        return out

    @property
    def ok(self) -> bool:
        return not self.all_issues()

    @property
    def blocked(self) -> bool:
        """A network/reachability failure -> STOP with BLOCKED, never launch."""
        return any(i.kind == "network" for i in self.all_issues())

    @property
    def failed(self) -> bool:
        """A provenance/freshness/identity failure -> MARKET_DATA_PROVENANCE_FAILED."""
        return any(i.kind != "network" for i in self.all_issues())

    def assert_ok(self) -> None:
        if self.ok:
            return
        joined = "; ".join(str(i) for i in self.all_issues())
        raise ProvenanceError(
            f"Market-data provenance gate FAILED: {joined}. Refusing to certify the "
            "feed for launch.")

    def to_dict(self) -> dict:
        return {
            "checked_utc": self.checked_utc,
            "interval": self.interval,
            "freshness_tolerance_s": self.freshness_tolerance_s,
            "min_warmup": self.min_warmup,
            "strict": self.strict,
            "source": self.source,
            "shared_boundary_epoch_ms": self.shared_boundary_epoch_ms,
            "ok": self.ok,
            "blocked": self.blocked,
            "failed": self.failed,
            "products": [p.to_dict() for p in self.products],
            "issues": [str(i) for i in self.issues],
        }


# --------------------------------------------------------------------------
# anti-mock: prove the request path is genuine
# --------------------------------------------------------------------------
def _genuine_requests_get():
    """Return the real ``requests.get`` or raise (so a missing lib is a network
    issue, and a monkeypatched one is a mock issue)."""
    import requests
    get = requests.get
    mod = getattr(get, "__module__", "") or ""
    if getattr(requests, "__name__", None) != "requests" or not mod.startswith("requests"):
        raise _MockDetected(
            f"requests.get is not the genuine library entry point "
            f"(module={mod!r}); refusing to trust a patched HTTP path.")
    return get


class _MockDetected(RuntimeError):
    pass


def _fetch_not_monkeypatched() -> Optional[str]:
    """Return an error message if PublicMarketData.fetch_ohlcv looks patched."""
    try:
        from ..data.public import PublicMarketData
    except Exception as e:  # pragma: no cover - defensive
        return f"could not import PublicMarketData to verify it is un-patched: {e}"
    fn = PublicMarketData.fetch_ohlcv
    qual = getattr(fn, "__qualname__", "")
    mod = getattr(fn, "__module__", "")
    if mod != "algotrading.data.public" or qual != "PublicMarketData.fetch_ohlcv":
        return (f"PublicMarketData.fetch_ohlcv appears monkeypatched "
                f"(module={mod!r}, qualname={qual!r}).")
    return None


# --------------------------------------------------------------------------
# per-product validation of the RAW response
# --------------------------------------------------------------------------
def _validate_rows(pp: ProductProvenance, rows, *, gran: int, now: datetime,
                   tolerance_s: int, min_warmup: int) -> Optional[int]:
    """Validate raw Coinbase candle rows. Returns the shared-boundary-eligible
    newest closed OPEN epoch (seconds) or None; appends issues to ``pp``."""
    if not isinstance(rows, list) or not rows:
        pp.issues.append(ProvenanceIssue(
            "plausibility", f"{pp.symbol}: empty/!list candle payload"))
        return None

    # Coinbase rows: [time, low, high, open, close, volume], newest first.
    parsed = []
    for r in rows:
        try:
            t, low, high, op, close, vol = (int(r[0]), float(r[1]), float(r[2]),
                                            float(r[3]), float(r[4]), float(r[5]))
        except (TypeError, ValueError, IndexError):
            pp.issues.append(ProvenanceIssue(
                "plausibility", f"{pp.symbol}: malformed candle row {r!r}"))
            return None
        parsed.append((t, low, high, op, close, vol))

    parsed.sort(key=lambda x: x[0])            # oldest-first
    epochs = [p[0] for p in parsed]

    # grid + strictly increasing + unique
    prev = None
    for e in epochs:
        if e % gran != 0:
            pp.issues.append(ProvenanceIssue(
                "grid", f"{pp.symbol}: candle open {e} not on the {gran}s grid"))
            return None
        if prev is not None and e <= prev:
            pp.issues.append(ProvenanceIssue(
                "grid", f"{pp.symbol}: candle {e} not strictly after {prev}"))
            return None
        prev = e

    now_epoch = int(now.timestamp())
    # Newest CLOSED candle: opened at t, closed at t+gran, and t+gran <= now.
    closed = [p for p in parsed if p[0] + gran <= now_epoch]
    if not closed:
        pp.issues.append(ProvenanceIssue(
            "freshness", f"{pp.symbol}: no closed candle at or before now"))
        return None
    newest = closed[-1]
    t, low, high, op, close, vol = newest
    close_epoch = t + gran
    age = now_epoch - close_epoch
    pp.newest_open_epoch_ms = t * 1000
    pp.newest_close_epoch_ms = close_epoch * 1000
    pp.newest_close_age_s = float(age)
    pp.newest_price = close
    pp.newest_volume = vol
    pp.candle_count = len(closed)

    if age > tolerance_s:
        pp.issues.append(ProvenanceIssue(
            "freshness",
            f"{pp.symbol}: newest closed candle is {age}s old (> {tolerance_s}s "
            "tolerance); data may be stale/cached"))

    # plausibility (structure, not a hard-coded price band)
    for name, v in (("open", op), ("high", high), ("low", low),
                    ("close", close), ("volume", vol)):
        import math
        if not math.isfinite(v):
            pp.issues.append(ProvenanceIssue(
                "plausibility", f"{pp.symbol}: non-finite {name}={v}"))
            return None
    if not (close > 0 and op > 0 and high > 0 and low > 0):
        pp.issues.append(ProvenanceIssue(
            "plausibility", f"{pp.symbol}: non-positive price on newest candle"))
    if vol <= 0:
        pp.issues.append(ProvenanceIssue(
            "plausibility", f"{pp.symbol}: non-positive volume {vol} on newest candle"))
    if not (low <= min(op, close) and high >= max(op, close) and high >= low):
        pp.issues.append(ProvenanceIssue(
            "plausibility",
            f"{pp.symbol}: OHLC out of order (o={op} h={high} l={low} c={close})"))

    if len(closed) < min_warmup:
        pp.issues.append(ProvenanceIssue(
            "coverage",
            f"{pp.symbol}: only {len(closed)} closed candles; need >= {min_warmup} "
            "to warm up every strategy"))

    return close_epoch - gran   # the newest closed candle's OPEN epoch (seconds)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------
def preflight(
    products: Dict[str, str],
    *,
    now: Optional[datetime] = None,
    interval: str = "1h",
    freshness_tolerance_s: int = DEFAULT_FRESHNESS_TOLERANCE_S,
    min_warmup: int = MIN_WARMUP,
    limit: int = 300,
    http_get: Optional[Callable[..., object]] = None,
    strict: bool = True,
) -> ProvenanceReport:
    """Run the keyless public provenance preflight and return a report.

    ``strict=True`` (launch mode) additionally proves the request path itself is
    genuine (real ``requests.get``, un-monkeypatched ``PublicMarketData``) and
    refuses an injected ``http_get``. ``strict=False`` lets the offline test suite
    drive the row validators with a synthetic ``http_get`` without tripping the
    anti-mock layer.
    """
    now = now or datetime.now(timezone.utc)
    gran = _GRANULARITY.get(interval)
    source = "injected" if http_get is not None else "network"
    report = ProvenanceReport(
        checked_utc=now.astimezone(timezone.utc).isoformat(),
        interval=interval, freshness_tolerance_s=freshness_tolerance_s,
        min_warmup=min_warmup, strict=strict, source=source)

    if gran is None:
        report.issues.append(ProvenanceIssue(
            "endpoint", f"unsupported interval {interval!r}"))
        return report

    # product identity: exactly the approved BTC-USD + ETH-USD set.
    if dict(products) != REQUIRED_PRODUCTS:
        report.issues.append(ProvenanceIssue(
            "product",
            f"products {dict(products)!r} are not exactly the approved set "
            f"{REQUIRED_PRODUCTS!r}"))
        # continue so the report still shows what each product returned

    # anti-mock (launch mode only)
    getter = http_get
    if strict:
        if http_get is not None:
            # A caller-provided fetch in launch mode cannot be trusted as genuine
            # market data. Refuse immediately — never fall through to a real
            # request that would mask the misuse.
            report.issues.append(ProvenanceIssue(
                "mock",
                "an injected http_get was supplied in strict/launch mode; the gate "
                "must perform its OWN genuine request, not a caller-provided one"))
            return report
        try:
            getter = _genuine_requests_get()
        except _MockDetected as e:
            report.issues.append(ProvenanceIssue("mock", str(e)))
            return report
        except Exception as e:
            report.issues.append(ProvenanceIssue(
                "network", f"could not load the requests library: {e}"))
            return report
        patched = _fetch_not_monkeypatched()
        if patched:
            report.issues.append(ProvenanceIssue("mock", patched))
    elif getter is None:
        try:
            getter = _genuine_requests_get()
        except Exception as e:
            report.issues.append(ProvenanceIssue(
                "network", f"could not load the requests library: {e}"))
            return report

    boundary_candidates: List[int] = []
    for symbol in sorted(products):
        product = products[symbol]
        url = COINBASE_BASE + CANDLES_PATH_TEMPLATE.format(product=product)
        pp = ProductProvenance(symbol=symbol, product=product, url=url)
        report.products.append(pp)
        try:
            resp = getter(url, params={"granularity": gran},
                          headers={"User-Agent": "algotrading/1.0"}, timeout=20)
        except Exception as e:
            pp.issues.append(ProvenanceIssue(
                "network", f"{symbol}: request to {url!r} failed: {e}"))
            continue

        pp.http_status = getattr(resp, "status_code", None)
        headers = dict(getattr(resp, "headers", {}) or {})
        pp.server_header = headers.get("Server")
        pp.content_type = headers.get("Content-Type")
        final_url = getattr(resp, "url", url) or url
        raw = getattr(resp, "content", None)
        if raw is None:
            text = getattr(resp, "text", "") or ""
            raw = text.encode("utf-8")
        pp.raw_bytes = len(raw)
        pp.raw_sha256 = sha256_bytes(raw)

        if pp.http_status != 200:
            pp.issues.append(ProvenanceIssue(
                "network", f"{symbol}: HTTP {pp.http_status} from {final_url!r}"))
            continue

        # endpoint identity from the response's OWN final URL.
        parts = urlsplit(final_url)
        if parts.scheme != COINBASE_SCHEME or parts.hostname != COINBASE_HOST:
            pp.issues.append(ProvenanceIssue(
                "endpoint",
                f"{symbol}: response came from {parts.scheme}://{parts.hostname} "
                f"not {COINBASE_SCHEME}://{COINBASE_HOST}"))
        expected_path = CANDLES_PATH_TEMPLATE.format(product=product)
        if parts.path != expected_path:
            pp.issues.append(ProvenanceIssue(
                "endpoint",
                f"{symbol}: response path {parts.path!r} != {expected_path!r}"))

        # server Date freshness (a static/cached file carries no fresh server Date).
        date_hdr = headers.get("Date")
        if date_hdr:
            try:
                server_dt = parsedate_to_datetime(date_hdr)
                if server_dt.tzinfo is None:
                    server_dt = server_dt.replace(tzinfo=timezone.utc)
                pp.server_date_utc = server_dt.astimezone(timezone.utc).isoformat()
                skew = abs((now - server_dt).total_seconds())
                if skew > freshness_tolerance_s:
                    pp.issues.append(ProvenanceIssue(
                        "freshness",
                        f"{symbol}: server Date {pp.server_date_utc} is {skew:.0f}s "
                        f"from now (> {freshness_tolerance_s}s); stale/cached?"))
            except (TypeError, ValueError):
                pass
        elif strict:
            pp.issues.append(ProvenanceIssue(
                "freshness",
                f"{symbol}: response carried no Date header to prove freshness"))

        try:
            rows = resp.json()
        except Exception as e:
            pp.issues.append(ProvenanceIssue(
                "plausibility", f"{symbol}: response body is not JSON: {e}"))
            continue

        newest_open = _validate_rows(
            pp, rows, gran=gran, now=now, tolerance_s=freshness_tolerance_s,
            min_warmup=min_warmup)
        if newest_open is not None:
            boundary_candidates.append(newest_open)
        pp.ok = not pp.issues

    # shared decision boundary present in EVERY product.
    if boundary_candidates and len(boundary_candidates) == len(products):
        report.shared_boundary_epoch_ms = min(boundary_candidates) * 1000
    return report


def assert_live_fetch_only(coord) -> None:
    """Fail closed if a coordinator would tick on an INJECTED fetch in launch mode.

    A live launch must consume the real ``PublicMarketData`` feed. If a fixture or
    caller wired ``fetch_ohlcv=`` into the coordinator, the market data is not
    provably genuine — refuse.
    """
    if getattr(coord, "_fetch_ohlcv", None) is not None:
        raise ProvenanceError(
            "Coordinator has an injected fetch_ohlcv; a live launch must use the "
            "genuine public market-data feed, not an injected one. Refusing.")
