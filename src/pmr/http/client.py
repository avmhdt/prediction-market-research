"""Async GET client shared by every collector (FR-12, NFR-01, NFR-07, NFR-12).

* Per-host leaky-bucket rate limits; a 429 halves the host's rate for 60 s.
* Retries with exponential backoff and full jitter on 429, 5xx, timeouts and connection
  errors, up to ``http.max_attempts`` attempts; ``Retry-After`` is honoured.
* Every request first passes :func:`pmr.http.guard.check_request`.
* An identifying ``User-Agent`` (``PMR_USER_AGENT``) on every request.
* Auth headers and secret query parameters are redacted from logs and metadata.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from pmr.http import kalshi_auth
from pmr.http.guard import check_request
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.settings import HttpConfig, PmrConfig, Settings, Venue

log = structlog.get_logger("pmr.http")

JSONValue = dict[str, Any] | list[Any]

REDACTED = "***"
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "x-dune-api-key",
        kalshi_auth.HEADER_KEY.lower(),
        kalshi_auth.HEADER_SIGNATURE.lower(),
        "cookie",
        "set-cookie",
    }
)
SECRET_PARAMS = frozenset({"api_key", "apikey", "token", "key", "access_token"})
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """Raised for a non-retryable HTTP status or after the last retry."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``headers`` with auth and cookie values replaced by ``***``."""
    return {k: (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


Params = Mapping[str, str | int] | Sequence[tuple[str, str | int]]
"""Query parameters: a mapping, or (key, value) pairs when a key repeats (Kalshi ``tickers``)."""


def redact_params(params: Params | None) -> dict[str, Any]:
    """Copy of query ``params`` with secret-looking keys replaced by ``***``.

    Repeated keys collapse into a list.
    """
    items = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    out: dict[str, Any] = {}
    for k, v in items:
        val = REDACTED if k.lower() in SECRET_PARAMS else v
        if k in out:
            prev = out[k]
            out[k] = [*prev, val] if isinstance(prev, list) else [prev, val]
        else:
            out[k] = val
    return out


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, dt.timestamp() - (time.time() if now is None else now))


def backoff_delay(attempt: int, cfg: HttpConfig, rng: random.Random) -> float:
    """Full-jitter exponential backoff for retry number ``attempt`` (1-based), seconds."""
    cap = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (attempt - 1)))
    return rng.uniform(0.0, cap)


def _httpx_params(params: Params | None) -> Any:
    if not params:
        return None
    return dict(params) if isinstance(params, Mapping) else list(params)


@dataclass(frozen=True)
class FetchResult:
    """One successful response plus the metadata stored with it in the raw archive (DQ-12)."""

    url: str
    params: dict[str, Any]
    status: int
    ts_recv_ns: int
    elapsed_s: float
    attempts: int
    date_header: str | None
    data: JSONValue
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def clock_offset_s(self) -> float | None:
        """Local clock minus the server ``Date`` header, in seconds (DQ-01); None if absent."""
        if not self.date_header:
            return None
        try:
            server = email.utils.parsedate_to_datetime(self.date_header).timestamp()
        except (TypeError, ValueError):
            return None
        return self.ts_recv_ns / 1e9 - server


Sleeper = Callable[[float], Awaitable[None]]


class HttpClient:
    """Guarded, rate-limited, retrying async GET client. Use as an async context manager."""

    def __init__(
        self,
        settings: Settings,
        http_cfg: HttpConfig,
        *,
        limits: RateLimiterRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
        seed: int = 0,
        extra_hosts: tuple[str, ...] = (),
    ) -> None:
        self.settings = settings
        self.cfg = http_cfg
        self.limits = limits or RateLimiterRegistry()
        self._sleep = sleep
        self._rng = random.Random(seed)
        self._extra_hosts = extra_hosts
        self._client = httpx.AsyncClient(
            timeout=http_cfg.timeout_s,
            headers={"User-Agent": settings.pmr_user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=False,
        )

    def allow_host(self, host: str) -> None:
        """Add a read-only data host (for example a configured subgraph) to the guard."""
        if host and host not in self._extra_hosts:
            self._extra_hosts = (*self._extra_hosts, host)

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def get(
        self,
        url: str,
        params: Params | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        sign_kalshi: bool = False,
        expect_json: bool = True,
    ) -> FetchResult:
        """GET ``url`` with retries; return the parsed body and response metadata.

        Raises:
            GuardError: the request is not an allowed read.
            HttpError: non-retryable status (4xx other than 429) or retries exhausted.
        """
        check_request("GET", url, extra_hosts=self._extra_hosts)
        host = (urlsplit(url).hostname or "").lower()
        limiter = self.limits.for_host(host)
        safe_params = redact_params(params)
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.max_attempts + 1):
            await limiter.acquire()
            req_headers = dict(headers or {})
            if not expect_json:
                # FRED answers 404 to "Accept: application/json" on its CSV endpoint.
                req_headers.setdefault("Accept", "text/csv, text/plain, */*")
            if sign_kalshi:
                req_headers.update(self._kalshi_headers(url))
            t0 = time.perf_counter()
            try:
                # An empty params dict would make httpx drop the query string already in ``url``.
                resp = await self._client.get(
                    url, params=_httpx_params(params), headers=req_headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning(
                    "http_transport_error",
                    url=url,
                    params=safe_params,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt < self.cfg.max_attempts:
                    await self._sleep(backoff_delay(attempt, self.cfg, self._rng))
                continue
            ts_recv_ns = time.time_ns()
            elapsed = time.perf_counter() - t0
            if resp.status_code in RETRY_STATUS:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                if resp.status_code == 429:
                    limiter.penalize()
                log.warning(
                    "http_retryable_status",
                    url=url,
                    params=safe_params,
                    status=resp.status_code,
                    attempt=attempt,
                    retry_after=retry_after,
                )
                last_exc = HttpError(
                    f"HTTP {resp.status_code}", status=resp.status_code, url=str(resp.url)
                )
                if attempt < self.cfg.max_attempts:
                    delay = backoff_delay(attempt, self.cfg, self._rng)
                    if retry_after is not None:
                        delay = max(delay, min(retry_after, self.cfg.backoff_max_s))
                    await self._sleep(delay)
                continue
            if resp.status_code >= 400:
                raise HttpError(
                    f"HTTP {resp.status_code} for {host}{urlsplit(url).path}",
                    status=resp.status_code,
                    url=url,
                )
            data: JSONValue
            if expect_json:
                parsed = resp.json()
                if not isinstance(parsed, dict | list):
                    raise HttpError(f"unexpected JSON type from {url}", status=resp.status_code)
                data = parsed
            else:
                data = {"text": resp.text}
            return FetchResult(
                url=url,
                params=safe_params,
                status=resp.status_code,
                ts_recv_ns=ts_recv_ns,
                elapsed_s=elapsed,
                attempts=attempt,
                date_header=resp.headers.get("Date"),
                data=data,
                headers=redact_headers(dict(resp.headers)),
            )
        status = last_exc.status if isinstance(last_exc, HttpError) else None
        raise HttpError(
            f"giving up on {host}{urlsplit(url).path} after {self.cfg.max_attempts} attempts",
            status=status,
            url=url,
        ) from last_exc

    def _kalshi_headers(self, url: str) -> dict[str, str]:
        s = self.settings
        if not (s.kalshi_api_key_id and s.kalshi_private_key_path):
            return {}
        return kalshi_auth.auth_headers(
            s.kalshi_api_key_id, s.kalshi_private_key_path, "GET", urlsplit(url).path
        )


def venue_base_url(cfg: PmrConfig, venue: Venue, service: str | None = None) -> str:
    """Base URL for a venue; Polymarket takes ``service`` in {gamma, clob, data}."""
    v = cfg.venues
    if venue == "kalshi":
        return v.kalshi.base_url
    if venue == "polymarket":
        svc = service or "gamma"
        urls = {
            "gamma": v.polymarket.gamma_url,
            "clob": v.polymarket.clob_url,
            "data": v.polymarket.data_url,
        }
        if svc not in urls:
            raise ValueError(f"unknown Polymarket service {svc!r}")
        return urls[svc]
    if venue == "manifold":
        return v.manifold.base_url
    if venue == "metaculus":
        return v.metaculus.base_url
    raise ValueError(f"unknown venue {venue!r}")


def rate_limits_from_config(cfg: PmrConfig) -> RateLimiterRegistry:
    """Per-host rate limits taken from ``config/pmr.yaml`` (NFR-01)."""
    v = cfg.venues
    per_host: dict[str, float] = {}

    def put(url: str, rps: float) -> None:
        per_host[(urlsplit(url).hostname or "").lower()] = rps

    put(v.kalshi.base_url, v.kalshi.rate_limit_rps)
    for url in (v.polymarket.gamma_url, v.polymarket.clob_url, v.polymarket.data_url):
        put(url, v.polymarket.rate_limit_rps)
    put(v.manifold.base_url, v.manifold.rate_limit_rps)
    put(v.metaculus.base_url, v.metaculus.rate_limit_rps)
    return RateLimiterRegistry(default_rps=1.0, per_host=per_host)


class VenueClient:
    """Venue-scoped client: ``await client.get("/markets", {"limit": 1})``.

    ``get`` returns the parsed JSON body. Several venues (Polymarket Gamma, Manifold) return
    a top-level JSON array, so the return type is ``dict | list`` (docs/decisions.md).
    """

    def __init__(
        self,
        venue: Venue,
        settings: Settings,
        *,
        config: PmrConfig,
        service: str | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.venue = venue
        self.settings = settings
        self.base_url = venue_base_url(config, venue, service).rstrip("/")
        self._own_http = http is None
        self.http = http or HttpClient(
            settings, config.http, limits=rate_limits_from_config(config), seed=config.seed
        )

    async def __aenter__(self) -> VenueClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http:
            await self.http.aclose()

    def _headers(self) -> dict[str, str]:
        if self.venue == "metaculus" and self.settings.metaculus_api_token is not None:
            return {
                "Authorization": f"Token {self.settings.metaculus_api_token.get_secret_value()}"
            }
        return {}

    async def fetch(self, path: str, params: Mapping[str, str | int] | None = None) -> FetchResult:
        """GET ``base_url + path`` and return the body with response metadata."""
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        return await self.http.get(
            url,
            params,
            headers=self._headers(),
            sign_kalshi=self.venue == "kalshi" and self.settings.kalshi_auth_configured,
        )

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> JSONValue:
        """GET ``base_url + path`` and return the parsed JSON body."""
        return (await self.fetch(path, params)).data
