"""FR-12 / NFR-01 / NFR-07: retries, Retry-After, 429 penalty, guard, redaction."""

from __future__ import annotations

import base64
import random
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from hypothesis import given
from hypothesis import strategies as st

from pmr.http.client import (
    FetchResult,
    HttpClient,
    HttpError,
    VenueClient,
    backoff_delay,
    parse_retry_after,
    rate_limits_from_config,
    redact_headers,
    redact_params,
    venue_base_url,
)
from pmr.http.guard import GuardError
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.settings import HttpConfig, PmrConfig, Settings

GAMMA = "https://gamma-api.polymarket.com"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def client(settings: Settings, cfg: PmrConfig, sleeps: Sleeps) -> HttpClient:
    return HttpClient(
        settings, cfg.http, limits=RateLimiterRegistry(default_rps=1000.0), sleep=sleeps
    )


@respx.mock
async def test_success_metadata_and_user_agent(settings: Settings, cfg: PmrConfig) -> None:
    route = respx.get(f"{GAMMA}/markets").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1}], headers={"Date": "Mon, 09 Nov 2026 12:00:00 GMT"}
        )
    )
    async with client(settings, cfg, Sleeps()) as c:
        r = await c.get(f"{GAMMA}/markets", {"limit": 1, "api_key": "zzz"})
    assert r.data == [{"id": 1}] and r.status == 200 and r.attempts == 1
    assert r.params == {"limit": 1, "api_key": "***"}
    assert r.clock_offset_s is not None
    req = route.calls.last.request
    assert req.headers["User-Agent"].startswith("prediction-market-research/0.1")
    assert req.url.params["limit"] == "1"


@respx.mock
async def test_query_in_url_is_kept_without_params(settings: Settings, cfg: PmrConfig) -> None:
    route = respx.get("https://fred.stlouisfed.org/graph/fredgraph.csv").mock(
        return_value=httpx.Response(200, text="observation_date,DTB3\n2026-11-06,4.0\n")
    )
    async with client(settings, cfg, Sleeps()) as c:
        r = await c.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3", expect_json=False
        )
    req = route.calls.last.request
    assert req.url.params["id"] == "DTB3"
    assert "application/json" not in req.headers["Accept"]
    assert isinstance(r.data, dict) and "DTB3" in r.data["text"]
    assert r.clock_offset_s is None


@respx.mock
async def test_429_retry_after_and_penalty(settings: Settings, cfg: PmrConfig) -> None:
    respx.get(f"{GAMMA}/markets").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps = Sleeps()
    async with client(settings, cfg, sleeps) as c:
        r = await c.get(f"{GAMMA}/markets")
        assert c.limits.for_host("gamma-api.polymarket.com").current_rps == 500.0
    assert r.attempts == 2
    assert sleeps.calls[0] >= 7.0


@respx.mock
async def test_5xx_and_timeouts_retry_then_give_up(settings: Settings, cfg: PmrConfig) -> None:
    respx.get(f"{GAMMA}/markets").mock(
        side_effect=[httpx.ReadTimeout("t"), httpx.ConnectError("c")] + [httpx.Response(503)] * 10
    )
    sleeps = Sleeps()
    async with client(settings, cfg, sleeps) as c:
        with pytest.raises(HttpError, match="after 6 attempts") as ei:
            await c.get(f"{GAMMA}/markets")
    assert ei.value.status == 503
    assert len(sleeps.calls) == cfg.http.max_attempts - 1
    assert all(0 <= s <= cfg.http.backoff_max_s for s in sleeps.calls)


@respx.mock
async def test_transport_errors_only(settings: Settings, cfg: PmrConfig) -> None:
    respx.get(f"{GAMMA}/markets").mock(side_effect=httpx.ConnectError("down"))
    async with client(settings, cfg, Sleeps()) as c:
        with pytest.raises(HttpError) as ei:
            await c.get(f"{GAMMA}/markets")
    assert ei.value.status is None


@respx.mock
async def test_4xx_is_not_retried(settings: Settings, cfg: PmrConfig) -> None:
    route = respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(404))
    async with client(settings, cfg, Sleeps()) as c:
        with pytest.raises(HttpError) as ei:
            await c.get(f"{GAMMA}/markets")
    assert ei.value.status == 404 and route.call_count == 1


@respx.mock
async def test_non_container_json_rejected(settings: Settings, cfg: PmrConfig) -> None:
    respx.get(f"{GAMMA}/x").mock(return_value=httpx.Response(200, json=3))
    async with client(settings, cfg, Sleeps()) as c:
        with pytest.raises(HttpError, match="unexpected JSON"):
            await c.get(f"{GAMMA}/x")


@respx.mock
async def test_guard_runs_before_any_request(settings: Settings, cfg: PmrConfig) -> None:
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    async with client(settings, cfg, Sleeps()) as c:
        with pytest.raises(GuardError):
            await c.get("https://external-api.kalshi.com/trade-api/v2/portfolio/balance")
        with pytest.raises(GuardError):
            await c.get("https://evil.example/markets")
    assert route.call_count == 0


@respx.mock
async def test_kalshi_signed_when_key_configured(tmp_path: Path, cfg: PmrConfig) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = tmp_path / "k.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        kalshi_api_key_id="kid",
        kalshi_private_key_path=str(pem),
    )
    base = cfg.venues.kalshi.base_url
    route = respx.get(f"{base}/historical/markets").mock(
        return_value=httpx.Response(200, json={"markets": []})
    )
    async with VenueClient("kalshi", s, config=cfg) as vc:
        data = await vc.get("historical/markets", {"limit": 2})
    assert data == {"markets": []}
    h = route.calls.last.request.headers
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/historical/markets".encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg)
    assert h["KALSHI-ACCESS-KEY"] == "kid"


@respx.mock
async def test_unsigned_by_default_and_metaculus_token(cfg: PmrConfig) -> None:
    s = Settings(_env_file=None, metaculus_api_token="tok")  # type: ignore[call-arg]
    k = respx.get(f"{cfg.venues.kalshi.base_url}/markets").mock(
        return_value=httpx.Response(200, json={"markets": []})
    )
    m = respx.get(f"{cfg.venues.metaculus.base_url}/questions/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    async with VenueClient("kalshi", s, config=cfg) as vc:
        await vc.get("/markets")
    async with VenueClient("metaculus", s, config=cfg) as vc:
        r = await vc.fetch("/questions/")
    assert isinstance(r, FetchResult)
    assert "KALSHI-ACCESS-SIGNATURE" not in k.calls.last.request.headers
    assert m.calls.last.request.headers["Authorization"] == "Token tok"


def test_venue_base_urls(cfg: PmrConfig) -> None:
    assert venue_base_url(cfg, "polymarket") == cfg.venues.polymarket.gamma_url
    assert venue_base_url(cfg, "polymarket", "clob") == cfg.venues.polymarket.clob_url
    assert venue_base_url(cfg, "polymarket", "data") == cfg.venues.polymarket.data_url
    assert venue_base_url(cfg, "manifold") == cfg.venues.manifold.base_url
    with pytest.raises(ValueError):
        venue_base_url(cfg, "polymarket", "nope")
    with pytest.raises(ValueError):
        venue_base_url(cfg, "betfair")  # type: ignore[arg-type]
    reg = rate_limits_from_config(cfg)
    assert reg.for_host("www.metaculus.com").base_rps == 2
    assert reg.for_host("clob.polymarket.com").base_rps == 5


def test_redaction() -> None:
    h = redact_headers(
        {
            "Authorization": "Token x",
            "KALSHI-ACCESS-SIGNATURE": "s",
            "X-Dune-Api-Key": "k",
            "A": "b",
        }
    )
    assert h == {
        "Authorization": "***",
        "KALSHI-ACCESS-SIGNATURE": "***",
        "X-Dune-Api-Key": "***",
        "A": "b",
    }
    assert redact_params({"token": "t", "limit": 3}) == {"token": "***", "limit": 3}
    assert redact_params(None) == {}


def test_retry_after_parsing() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Mon, 09 Nov 2026 12:00:10 GMT", now=1794225600.0) == 10.0


@given(st.integers(min_value=1, max_value=30), st.integers(min_value=0, max_value=10_000))
def test_backoff_bounded(attempt: int, seed: int) -> None:
    cfg = HttpConfig(timeout_s=1, max_attempts=6, backoff_base_s=1.0, backoff_max_s=60)
    d = backoff_delay(attempt, cfg, random.Random(seed))
    assert 0.0 <= d <= min(60.0, 2 ** (attempt - 1))
