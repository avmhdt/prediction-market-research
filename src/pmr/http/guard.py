"""Request guard: the project is read-only and paper-only (FR-12, AC-W-10).

Every outgoing request passes :func:`check_request` before it is sent. The guard raises
:class:`GuardError` when

* the host is not on the allowlist,
* the method is anything but ``GET`` (the single exception is Dune query execution,
  ``POST /api/v1/query/<id>/execute`` on ``api.dune.com``),
* any path segment names an order, portfolio, cancel, auth or wallet endpoint.

``/order`` is matched as a whole path segment, so the public order-book reads
(Kalshi ``/markets/<ticker>/orderbook``, Polymarket ``/book``) stay allowed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

VENUE_HOSTS: frozenset[str] = frozenset(
    {
        "external-api.kalshi.com",
        "api.elections.kalshi.com",
        "trading-api.kalshi.com",
        "gamma-api.polymarket.com",
        "clob.polymarket.com",
        "data-api.polymarket.com",
        "polymarket.com",
        "api.manifold.markets",
        "www.metaculus.com",
        "metaculus.com",
    }
)
"""Hosts of the prediction-market venues: GET only, never an order or portfolio path."""

DATA_HOSTS: frozenset[str] = frozenset(
    {
        "fred.stlouisfed.org",
        "api.dune.com",
        "gateway.thegraph.com",
        "api.goldsky.com",
    }
)
"""Public data hosts that are not venues."""

DUNE_EXECUTE = re.compile(r"^/api/v1/query/\d+/execute$")

# Path segments that identify trading, account or credential endpoints on any venue.
_FORBIDDEN_SEGMENT = re.compile(
    r"^(order|orders|portfolio|positions-admin|cancel|cancel-all|cancel-orders|"
    r"cancel-market-orders|auth|api-key|api-keys|derive-api-key|balance-allowance|"
    r"wallet|wallets|withdraw|withdrawals|deposit|deposits|transfer|transfers)$",
    re.IGNORECASE,
)
_FORBIDDEN_PREFIX = re.compile(r"^(portfolio|order)[-_]", re.IGNORECASE)


class GuardError(PermissionError):
    """Raised when a request would leave the read-only, paper-only envelope."""


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def check_request(method: str, url: str, *, extra_hosts: Iterable[str] = ()) -> None:
    """Raise :class:`GuardError` unless ``method url`` is an allowed read.

    Args:
        method: HTTP method, any case.
        url: absolute URL including scheme and host.
        extra_hosts: additional allowed data hosts (for example a configured subgraph host).
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise GuardError(f"only https is allowed: {url!r}")
    host = (parts.hostname or "").lower()
    allowed = VENUE_HOSTS | DATA_HOSTS | {h.lower() for h in extra_hosts}
    if host not in allowed:
        raise GuardError(f"host {host!r} is not on the allowlist")
    m = method.upper()
    path = parts.path or "/"
    if m != "GET":
        if m == "POST" and host == "api.dune.com" and DUNE_EXECUTE.match(path):
            return
        raise GuardError(f"method {m} is not allowed for {host}{path}")
    for seg in _segments(path):
        if _FORBIDDEN_SEGMENT.match(seg) or _FORBIDDEN_PREFIX.match(seg):
            raise GuardError(f"path {path!r} names a trading or account endpoint")
