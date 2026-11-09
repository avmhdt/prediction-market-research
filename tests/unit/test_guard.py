"""AC-W-10: the guard refuses writes and order/portfolio paths on every venue host."""

from __future__ import annotations

import pytest

from pmr.http.guard import VENUE_HOSTS, GuardError, check_request

KALSHI = "https://external-api.kalshi.com/trade-api/v2"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "OPTIONS", "HEAD"])
@pytest.mark.parametrize("host", sorted(VENUE_HOSTS))
def test_non_get_to_venue_host_raises(method: str, host: str) -> None:
    with pytest.raises(GuardError):
        check_request(method, f"https://{host}/markets")


@pytest.mark.parametrize(
    "url",
    [
        f"{KALSHI}/portfolio/orders",
        f"{KALSHI}/portfolio/balance",
        f"{KALSHI}/portfolio",
        f"{KALSHI}/orders",
        f"{KALSHI}/order/abc",
        "https://clob.polymarket.com/order",
        "https://clob.polymarket.com/orders",
        "https://clob.polymarket.com/cancel-all",
        "https://clob.polymarket.com/auth/derive-api-key",
        "https://clob.polymarket.com/PORTFOLIO",
        "https://api.manifold.markets/v0/order_book_fake/x/portfolio-x",
    ],
)
def test_order_and_portfolio_paths_raise_even_for_get(url: str) -> None:
    with pytest.raises(GuardError, match="trading or account"):
        check_request("GET", url)


@pytest.mark.parametrize(
    "url",
    [
        f"{KALSHI}/markets?limit=1",
        f"{KALSHI}/markets/KXFED-26DEC-T4.25/orderbook",
        f"{KALSHI}/markets/trades",
        f"{KALSHI}/historical/cutoff",
        "https://gamma-api.polymarket.com/markets",
        "https://clob.polymarket.com/book?token_id=1",
        "https://clob.polymarket.com/prices-history",
        "https://data-api.polymarket.com/v2/trades",
        "https://api.manifold.markets/v0/bets",
        "https://www.metaculus.com/api/posts/",
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3",
    ],
)
def test_public_reads_pass(url: str) -> None:
    check_request("GET", url)


def test_unknown_host_raises() -> None:
    with pytest.raises(GuardError, match="allowlist"):
        check_request("GET", "https://example.com/markets")


def test_extra_host_is_allowed_for_reads_only() -> None:
    check_request("GET", "https://sub.example.org/q", extra_hosts=["sub.example.org"])
    with pytest.raises(GuardError):
        check_request("POST", "https://sub.example.org/q", extra_hosts=["sub.example.org"])


def test_plain_http_raises() -> None:
    with pytest.raises(GuardError, match="https"):
        check_request("GET", "http://gamma-api.polymarket.com/markets")


def test_only_dune_execute_post_is_allowed() -> None:
    check_request("POST", "https://api.dune.com/api/v1/query/12345/execute")
    for url in (
        "https://api.dune.com/api/v1/query/12345/results",
        "https://api.dune.com/api/v1/query/abc/execute",
        "https://api.dune.com/api/v1/query/1/execute/x",
    ):
        with pytest.raises(GuardError):
            check_request("POST", url)
