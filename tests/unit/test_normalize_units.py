"""Unit and property tests for conversions, categories and the normalizers' edge cases."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmr.normalize import kalshi, manifold, polymarket
from pmr.normalize.categories import CategoryMap, load
from pmr.normalize.util import fnum, iso_ns, json_list, rules_hash, stable_id, unix_ms_ns, unix_s_ns
from pmr.warehouse.build import version_markets
from tests.conftest import REPO

CATS = load(str(REPO / "config" / "categories.yaml"))


def test_time_conversions() -> None:
    assert iso_ns("2024-06-01T00:00:00Z") == 1_717_200_000 * 10**9
    assert iso_ns("2024-06-01 00:00:00+00") == 1_717_200_000 * 10**9
    assert iso_ns("2024-06-01T02:00:00+02:00") == 1_717_200_000 * 10**9
    assert iso_ns("2024-06-01") == 1_717_200_000 * 10**9
    assert iso_ns("") is None and iso_ns(None) is None and iso_ns("garbage") is None
    assert unix_s_ns("1717200000") == 1_717_200_000 * 10**9
    assert unix_ms_ns(1_717_200_000_000) == 1_717_200_000 * 10**9
    assert unix_ms_ns(13_569_418_800_000_000) is None  # year 2400: outside int64 ns
    assert unix_s_ns(None) is None and unix_ms_ns("") is None


def test_small_helpers() -> None:
    assert fnum("0.5") == 0.5 and fnum("x") is None and fnum(None) is None
    assert json_list('["a", "b"]') == ["a", "b"] and json_list([1]) == [1]
    assert json_list("[broken") == [] and json_list('"x"') == [] and json_list(3) == []
    assert rules_hash("a", None) == rules_hash("a", "")
    assert stable_id(1, None) == stable_id(1, "") and len(stable_id("x")) == 24


@pytest.mark.parametrize(
    ("venue", "labels", "expected"),
    [
        ("kalshi", ["Economics"], "economics"),
        ("kalshi", ["Climate and Weather"], "weather-climate"),
        ("polymarket", ["All", "NBA", "Sports"], "sports"),
        ("polymarket", ["US-current-affairs"], "politics"),
        ("polymarket", ["Bitcoin"], "crypto"),
        ("polymarket", ["Fed Rates"], "economics"),
        ("manifold", ["us-politics"], "politics"),
        ("manifold", ["ai"], "science-tech"),
        ("manifold", ["fun", "personal"], "other"),
        ("manifold", [None, ""], "other"),
    ],
)
def test_categories(venue: str, labels: list[str | None], expected: str) -> None:
    assert CATS.map(venue, labels) == expected


def test_category_targets_must_be_in_taxonomy() -> None:
    with pytest.raises(ValueError, match="taxonomy"):
        CategoryMap.from_dict({"taxonomy": ["a"], "exact": {"x": {"k": "b"}}})


def gm(**kw: Any) -> dict[str, Any]:
    base = {
        "conditionId": "0xc",
        "clobTokenIds": '["1", "2"]',
        "outcomes": '["Yes", "No"]',
        "closed": True,
        "outcomePrices": '["1", "0"]',
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("kw", "status", "result"),
    [
        ({}, "resolved", "YES"),
        ({"outcomePrices": '["0", "1"]'}, "resolved", "NO"),
        ({"outcomePrices": '["0.5", "0.5"]'}, "resolved", "SCALAR"),
        ({"outcomePrices": '["0", "0"]'}, "closed", None),
        (
            {"outcomePrices": '["0.2", "0.8"]', "umaResolutionStatuses": '["disputed"]'},
            "disputed",
            None,
        ),
        ({"closed": False, "outcomePrices": '["0.2", "0.8"]'}, "open", None),
    ],
)
def test_polymarket_market_status(kw: dict[str, Any], status: str, result: str | None) -> None:
    row = polymarket.market_row(gm(**kw), 1, CATS)
    assert row is not None and row["status"] == status and row["result"] == result


def test_polymarket_market_without_two_tokens_is_skipped() -> None:
    assert polymarket.market_row(gm(clobTokenIds="[]"), 1, CATS) is None
    rec = {"ts_recv_ns": 1, "body": {"markets": [gm(clobTokenIds="[]"), "junk"]}}
    assert polymarket.markets(rec, CATS) == []


@given(st.floats(min_value=0.0, max_value=1.0), st.booleans(), st.integers(0, 1))
def test_polymarket_orientation_is_an_involution(p: float, buy: bool, idx: int) -> None:
    rec = {
        "ts_recv_ns": 1,
        "key": {"condition_id": "0xc"},
        "body": {
            "data": [
                {
                    "price": p,
                    "size": 1,
                    "side": "BUY" if buy else "SELL",
                    "outcome_index": idx,
                    "token_id": "t",
                    "timestamp": 1,
                }
            ]
        },
    }
    (row,) = polymarket.trades(rec)
    # Mirroring the print (other outcome, other side, 1 - price) gives the same YES trade.
    rec["body"]["data"][0].update(price=1 - p, outcome_index=1 - idx, side="SELL" if buy else "BUY")
    (mirror,) = polymarket.trades(rec)
    assert abs(row["price"] - mirror["price"]) < 1e-12 and row["side"] == mirror["side"]
    assert 0.0 <= row["price"] <= 1.0


@given(st.lists(st.tuples(st.integers(1, 99), st.integers(1, 1000)), max_size=5))
def test_kalshi_no_bids_become_yes_asks(no_bids: list[tuple[int, int]]) -> None:
    rec = {"ts_recv_ns": 1, "key": {"ticker": "T"}, "body": {"orderbook": {"no": no_bids}}}
    rows = kalshi.book(rec, 0)
    assert len(rows) == len(no_bids)
    for (q, size), r in zip(no_bids, rows, strict=True):
        assert r["side"] == -1 and abs(r["price"] - (1 - q / 100)) < 1e-9 and r["qty"] == size


def test_kalshi_market_statuses_and_cents() -> None:
    m = {"ticker": "T", "status": "active", "result": ""}
    assert kalshi.market_row(m, 1, CATS)["status"] == "open"
    m = {"ticker": "T", "status": "settled", "result": "void"}
    assert kalshi.market_row(m, 1, CATS)["status"] == "voided"
    m = {"ticker": "T", "status": "settled", "result": "scalar", "settlement_value": 37}
    row = kalshi.market_row(m, 1, CATS)
    assert row["result"] == "SCALAR" and row["settlement_value"] == 0.37
    assert kalshi.market_row({"ticker": "T", "status": "closed"}, 1, CATS)["status"] == "closed"
    assert kalshi.dollars({"yes_bid": 55}, "yes_bid") == 0.55
    assert kalshi.dollars({"yes_bid_dollars": "0.5600", "yes_bid": 55}, "yes_bid") == 0.56
    assert kalshi.dollars({}, "yes_bid") is None


def test_manifold_skips_non_binary_and_bad_bets() -> None:
    assert manifold.market_row({"id": "x", "outcomeType": "FREE_RESPONSE"}, 1, CATS) is None
    rec = {
        "ts_recv_ns": 1,
        "body": [
            {
                "id": "a",
                "contractId": "m",
                "amount": 0,
                "shares": 0,
                "outcome": "YES",
                "createdTime": 1,
                "probAfter": 0.4,
            },
            {
                "id": "b",
                "contractId": "m",
                "amount": 30,
                "shares": 10,
                "outcome": "YES",
                "createdTime": 1,
            },
            {"id": "c", "isCancelled": True},
        ],
    }
    trades, ticks = manifold.bets(rec)
    assert trades == [] and len(ticks) == 1  # zero-share limit order and price > 1 skipped


def _m(symbol: str, valid_from: int, rules: str | None, title: str = "t") -> dict[str, Any]:
    from pmr.normalize.schema import MARKETS

    row: dict[str, Any] = dict.fromkeys(MARKETS)
    row.update(
        venue="manifold",
        symbol=symbol,
        market_id=symbol,
        rules_text=rules,
        rules_hash=rules_hash(rules) if rules else None,
        category="other",
        status="open",
        contract_type="binary_event",
        is_play_money=True,
        is_forecast=False,
        disputed=False,
        title=title,
        valid_from=valid_from,
        ingested_at=valid_from,
    )
    return row


def test_versioning_collapses_drops_lite_and_flags_rule_changes() -> None:
    from pmr.normalize.schema import MARKETS, frame

    rows = [
        _m("a", 1, None),  # lite: dropped because a full record exists
        _m("a", 2, "rules v1"),
        _m("a", 3, "rules v1"),  # identical: collapsed
        _m("a", 4, "rules v2"),  # rules change after listing (DQ-03)
        _m("b", 1, None),  # only lite records: kept
    ]
    out = version_markets(frame("markets", rows), pl.DataFrame(schema=MARKETS), CATS)
    a = out.filter(pl.col("symbol") == "a").sort("valid_from")
    assert a["valid_from"].to_list() == [2, 4]
    assert a["valid_to"].to_list() == [4, None]
    assert a["rules_changed"].to_list() == [False, True]
    assert out.filter(pl.col("symbol") == "b").height == 1
    empty = version_markets(frame("markets", []), pl.DataFrame(schema=MARKETS), CATS)
    assert empty.is_empty() and "valid_to" in empty.columns
