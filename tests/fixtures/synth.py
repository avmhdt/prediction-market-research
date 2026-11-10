"""Synthetic venue responses shaped like the documented APIs (field names copied, values invented).

No captured venue response is committed (SPEC section 11). Every builder is deterministic.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
MANIFOLD = "https://api.manifold.markets/v0"
METACULUS = "https://www.metaculus.com/api"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"

T0 = 1_717_200_000  # 2024-06-01 00:00 UTC
CUTOFF_ISO = "2024-06-10T00:00:00Z"
CUTOFF = 1_717_977_600


def gamma_market(i: int, *, yes_won: bool = True, closed: bool = True) -> dict[str, Any]:
    """One Gamma market (JSON-encoded list fields, as the API returns them)."""
    tokens = [f"{1000 + i}111", f"{1000 + i}222"]
    return {
        "id": str(500 + i),
        "conditionId": f"0xc{i:03d}",
        "question": f"Will synthetic event {i} happen?",
        "description": f"Resolves Yes if event {i} happens by June 30, 2024, 11:59 PM ET.",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]' if yes_won else '["0", "1"]',
        "clobTokenIds": json.dumps(tokens),
        "closed": closed,
        "startDate": "2024-05-01T00:00:00Z",
        "endDate": "2024-06-20T00:00:00Z",
        "closedTime": "2024-06-21 12:00:00+00",
        "umaResolutionStatuses": '["proposed", "disputed", "resolved"]' if i == 2 else "[]",
        "volumeNum": 1500.0 + i,
        "category": None,
        "tags": [{"label": "Sports" if i % 2 else "Politics"}, {"label": "All"}],
        "events": [{"id": str(9000 + i // 2), "title": f"Event group {i // 2}"}],
    }


def gamma_page(after: str | None, per_page: int = 2, n: int = 5) -> dict[str, Any]:
    """Keyset page: cursors are the next start index as a string."""
    start = int(after or 0)
    idx = list(range(start, min(n, start + per_page)))
    body: dict[str, Any] = {"markets": [gamma_market(i, yes_won=i % 3 != 0) for i in idx]}
    if start + per_page < n:
        body["next_cursor"] = str(start + per_page)
    return body


def v2_trades(condition: str, cursor: str | None) -> dict[str, Any]:
    """Two pages of Data API v2 trades for a condition: YES and NO prints."""
    i = int(condition.removeprefix("0xc"))
    page = int(cursor or 0)
    rows = []
    for k in range(3):
        n = page * 3 + k
        rows.append(
            {
                "proxy_wallet": f"0xw{n}",
                "side": "BUY" if k != 1 else "SELL",
                "token_id": f"{1000 + i}{'111' if k != 2 else '222'}",
                "condition_id": condition,
                "size": 10.0 + n,
                "price": 0.6 if k != 2 else 0.35,
                "timestamp": T0 + 3600 * (n + 1),
                "outcome": "Yes" if k != 2 else "No",
                "outcome_index": 0 if k != 2 else 1,
                "transaction_hash": f"0xtx{i}_{n}",
            }
        )
    more = page == 0
    return {
        "data": rows,
        "pagination": {"limit": 1000, "has_more": more, "next_cursor": "1" if more else None},
    }


def kalshi_market(
    ticker: str, *, result: str = "yes", close: str = "2024-06-05T00:00:00Z"
) -> dict[str, Any]:
    """One Kalshi market with dollar-string prices."""
    return {
        "ticker": ticker,
        "event_ticker": "KXSYN-24JUN",
        "market_type": "binary",
        "title": f"Synthetic {ticker}?",
        "yes_sub_title": "Above 4.00%",
        "open_time": "2024-05-01T00:00:00Z",
        "close_time": close,
        "expected_expiration_time": close,
        "status": "finalized",
        "result": result,
        "rules_primary": "If the rate is above 4.00% on June 5, 2024, resolves Yes.",
        "rules_secondary": "Source: synthetic.",
        "tick_size": 1,
        "volume": 250,
    }


def kalshi_trades(ticker: str, source: str, cursor: str | None) -> dict[str, Any]:
    """One or two pages of Kalshi trades; historical before the cutoff, live after it."""
    base = T0 if source == "historical" else CUTOFF + 3600
    page = int(cursor or 0)
    trades = [
        {
            "trade_id": f"{ticker}-{source}-{page}-{k}",
            "ticker": ticker,
            "count": 5 + k,
            "yes_price_dollars": "0.6200",
            "no_price_dollars": "0.3800",
            "taker_side": "yes" if k == 0 else "no",
            "created_time": f"2024-06-0{1 + k}T00:00:00Z"
            if source == "historical"
            else ("2024-06-11T00:00:00Z"),
        }
        for k in range(2)
    ]
    _ = base
    return {"trades": trades, "cursor": "1" if page == 0 else ""}


def manifold_lite(i: int) -> dict[str, Any]:
    """Lite market record."""
    return {
        "id": f"mf{i}",
        "question": f"Manifold synthetic {i}?",
        "outcomeType": "BINARY" if i != 3 else "MULTIPLE_CHOICE",
        "createdTime": (T0 - 86400 * 30) * 1000,
        "closeTime": (T0 + 86400 * i) * 1000,
        "isResolved": True,
        "resolution": ["YES", "NO", "CANCEL", "YES", "MKT"][i % 5],
        "resolutionProbability": 0.42,
        "resolutionTime": (T0 + 86400 * i + 60) * 1000,
        "uniqueBettorCount": 12 if i != 4 else 3,
        "volume": 500.0,
    }


def manifold_full(i: int) -> dict[str, Any]:
    """Full market record (adds groups and rules text)."""
    return {**manifold_lite(i), "groupSlugs": ["us-politics"], "textDescription": f"Rules {i}."}


def manifold_bets(mid: str, before: str | None) -> list[dict[str, Any]]:
    """Bets newest first; ``before`` paging; PAGE is patched to 2 in tests."""
    all_bets = [
        {
            "id": f"{mid}-b{k}",
            "contractId": mid,
            "createdTime": (T0 + 60 * (10 - k)) * 1000,
            "amount": 10.0 if k != 2 else -5.0,
            "shares": 20.0 if k != 2 else 8.0,
            "outcome": "YES" if k % 2 == 0 else "NO",
            "probBefore": 0.5,
            "probAfter": 0.5 + 0.01 * k,
            "isRedemption": k == 3,
        }
        for k in range(4)
    ]
    start = 0 if before is None else [b["id"] for b in all_bets].index(before) + 1
    return all_bets[start : start + 2]


def metaculus_page(offset: int) -> dict[str, Any]:
    """Posts page with ``next`` links; one binary question per page."""
    q = {
        "id": 100 + offset,
        "type": "binary",
        "title": f"Metaculus synthetic {offset}?",
        "resolution": "yes" if offset == 0 else "annulled",
        "open_time": "2024-01-01T00:00:00Z",
        "scheduled_close_time": "2024-06-01T00:00:00Z",
        "actual_close_time": "2024-06-01T00:00:00Z",
        "scheduled_resolve_time": "2024-06-02T00:00:00Z",
        "actual_resolve_time": "2024-06-02T00:00:00Z",
        "resolution_criteria": "Resolves yes if ...",
        "fine_print": "",
        "aggregations": {
            "recency_weighted": {
                "history": [
                    {
                        "start_time": T0 - 7200,
                        "end_time": T0 - 3600,
                        "centers": [0.7],
                        "forecaster_count": 30,
                    },
                    {
                        "start_time": T0 - 3600,
                        "end_time": None,
                        "centers": [0.75],
                        "forecaster_count": 31,
                    },
                ]
            }
        },
    }
    nxt = f"{METACULUS}/posts/?limit=100&offset={offset + 1}" if offset == 0 else None
    return {
        "next": nxt,
        "results": [{"id": 200 + offset, "title": q["title"], "question": q, "nr_forecasters": 31}],
    }


FRED_CSV = "observation_date,DTB3\n2024-06-03,5.25\n2024-06-04,\n2024-06-05,5.24\n"


def param(request: httpx.Request, name: str) -> str | None:
    """One query parameter of a mocked request."""
    return request.url.params.get(name)
