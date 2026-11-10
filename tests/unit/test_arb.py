"""AC-W-02: the scanner's components, a hand-computed golden pair, and its invariants."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmr.analysis.arb import (
    ArbConfig,
    ContractPair,
    LegInfo,
    buy_no_levels,
    buy_yes_levels,
    haircut,
    scan_multi_outcome,
    scan_pair,
    scan_yes_no,
    to_frame,
)
from pmr.analysis.lockup import RateCurve, days_to_resolution, lockup_cost
from pmr.fees.table import FeeTable
from tests.conftest import REPO

FEES = FeeTable.load(REPO / "config" / "fees.yaml")
TS = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp()) * 10**9
DAY = 86_400 * 10**9


def book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> pl.DataFrame:
    rows = [(1, p, q) for p, q in bids] + [(-1, p, q) for p, q in asks]
    return pl.DataFrame(rows, schema=["side", "price", "qty"], orient="row")


def cfg(**kw: object) -> ArbConfig:
    base: dict[str, object] = {
        "fees": FEES,
        "r_annual": 0.04,
        "settlement_lag_hours": {"kalshi": 24.0, "polymarket": 24.0},
    }
    base.update(kw)
    return ArbConfig(**base)  # type: ignore[arg-type]


def test_lockup_closed_form_and_days() -> None:
    assert lockup_cost(100.0, 365.0, 0.05) == pytest.approx(5.0)
    assert lockup_cost(100.0, 365.0 / 2, 0.05) == pytest.approx(100 * (1.05**0.5 - 1))
    assert lockup_cost(100.0, 365.0, 0.05, 0.05) == 0.0
    assert lockup_cost(100.0, 0.0, 0.05) == 0.0
    assert days_to_resolution(0, 10 * DAY, 12 * DAY, lag_a_hours=24, lag_b_hours=0) == 12.0
    assert days_to_resolution(20 * DAY, 10 * DAY) == 0.0
    curve = RateCurve([date(2026, 11, 5), date(2026, 11, 6)], [4.0, 4.01])
    assert curve.at(date(2026, 11, 9)) == pytest.approx(0.0401)
    with pytest.raises(ValueError):
        curve.at(date(2026, 1, 1))


def test_levels_and_haircut() -> None:
    b = book([(0.40, 5), (0.39, 7)], [(0.45, 3), (0.47, 0)])
    assert buy_yes_levels(b) == [(0.45, 3.0)]
    assert buy_no_levels(b) == [(pytest.approx(0.60), 5.0), (pytest.approx(0.61), 7.0)]
    c = cfg(divergence_counts={"deadline": (1, 8)})
    assert haircut(["deadline"], c) == pytest.approx(2 / 10)
    assert haircut(["source"], c) == pytest.approx(0.5)  # Beta(1, 1) prior, no data yet


def test_golden_deadline_mismatch_pair_hand_computed() -> None:
    """Kalshi YES ask 0.40 x 100, Polymarket YES bid 0.55 x 100 (NO ask 0.45), orientation +1,
    one hand-confirmed deadline difference, catalogue base rate 1 of 9."""
    a = LegInfo("kalshi", "KX-A", TS + 30 * DAY)
    b = LegInfo("polymarket", "pm-1", TS + 31 * DAY, category="economics")
    pair = ContractPair("pair-1", a, b, 1, ("deadline",))
    c = cfg(
        divergence_counts={"deadline": (1, 9)},
        transfer_cost_per_contract=0.002,
        collateral_yield={"kalshi": 0.0, "polymarket": 0.0},
    )
    rows = scan_pair(book([], [(0.40, 100)]), book([(0.55, 100)], []), pair, TS, c)
    assert len(rows) == 1
    r = rows[0]
    # Hand computation.
    k_model = Decimal("0.07") * 100 * Decimal("0.40") * Decimal("0.60")  # 1.68
    k_trade = k_model.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    k_change = Decimal("-40") - k_trade
    k_fee = Decimal("-40") - k_change.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    p_fee = (Decimal("0.05") * 100 * Decimal("0.45") * Decimal("0.55")).quantize(Decimal("0.00001"))
    days = (31 * DAY + 24 * 3600 * 10**9) / DAY  # later resolution plus its lag = 32 days
    lock = 0.40 * (1.04 ** (days / 365) - 1) + 0.45 * (1.04 ** (days / 365) - 1)
    hc = (1 + 1) / (9 + 2) * 1.0
    net = (1 - 0.85) - float(k_fee) / 100 - float(p_fee) / 100 - lock - hc - 0.002
    assert r.direction == "yes_a_no_b" and r.kind == "cross_venue" and r.size == 100
    assert r.cost == pytest.approx(0.85, abs=1e-12)
    assert r.fee_a == pytest.approx(float(k_fee) / 100, abs=1e-12)
    assert r.fee_b == pytest.approx(float(p_fee) / 100, abs=1e-12)
    assert r.days_to_resolution == pytest.approx(32.0)
    assert r.lockup_cost == pytest.approx(lock, abs=1e-12)
    assert r.resolution_haircut == pytest.approx(hc, abs=1e-12)
    assert abs(r.net_edge - net) < 1e-9
    assert r.net_annualized == pytest.approx(r.net_edge / r.cost * 365 / 32)


def test_orientation_minus_one_and_no_edge() -> None:
    a = LegInfo("kalshi", "A", TS + DAY)
    b = LegInfo("kalshi", "B", TS + DAY)
    pair = ContractPair("p", a, b, -1)
    rows = scan_pair(book([], [(0.30, 10)]), book([], [(0.60, 10)]), pair, TS, cfg())
    assert {r.direction for r in rows} == {"yes_a_yes_b"}
    assert scan_pair(book([], [(0.6, 10)]), book([], [(0.6, 10)]), pair, TS, cfg()) == []
    assert scan_pair(book([], []), book([], [(0.1, 1)]), pair, TS, cfg()) == []


def test_walk_breaks_at_each_level_and_stops_when_unprofitable() -> None:
    a = LegInfo("polymarket", "Y", TS + 10 * DAY, category="sports")
    rows = scan_yes_no(
        a, [(0.40, 10), (0.45, 10), (0.60, 10)], [(0.50, 15), (0.52, 100)], TS, cfg()
    )
    assert [r.size for r in rows] == [10.0, 15.0, 20.0]
    assert rows[0].cost == pytest.approx(0.90)
    assert all(r.kind == "yes_no" for r in rows)
    frame = to_frame(rows)
    assert frame.height == 3 and "net_annualized" in frame.columns


def test_multi_outcome_both_directions() -> None:
    legs = [
        (
            LegInfo("polymarket", f"o{i}", TS + 5 * DAY, category="politics"),
            book([(bid, 50)], [(ask, 50)]),
        )
        for i, (bid, ask) in enumerate([(0.30, 0.31), (0.30, 0.31), (0.30, 0.32)])
    ]
    rows = scan_multi_outcome("ev", legs, TS, cfg())
    yes = [r for r in rows if r.direction == "buy_all_yes"]
    assert yes and yes[0].cost == pytest.approx(0.94)
    assert not [r for r in rows if r.direction == "buy_all_no"]  # 3 x 0.70 = 2.10 > 2
    rich = [(info, book([(0.40, 50)], [(0.45, 50)])) for info, _ in legs]
    rows = scan_multi_outcome("ev", rich, TS, cfg())
    no = [r for r in rows if r.direction == "buy_all_no"]
    assert no and no[0].gross_edge == pytest.approx(2 - 3 * 0.60)
    assert scan_multi_outcome("ev", legs[:1], TS, cfg()) == []


levels = st.lists(st.tuples(st.floats(0.01, 0.99), st.floats(0.1, 500.0)), min_size=1, max_size=4)


@settings(max_examples=60, deadline=None)
@given(levels, levels, st.integers(0, 3), st.integers(0, 10), st.floats(0.0, 0.01))
def test_net_edge_identity_and_no_free_lunch(
    asks_a: list[tuple[float, float]],
    bids_b: list[tuple[float, float]],
    k: int,
    n: int,
    transfer: float,
) -> None:
    a = LegInfo("kalshi", "A", TS + 20 * DAY)
    b = LegInfo("polymarket", "B", TS + 21 * DAY, category="crypto")
    pair = ContractPair("p", a, b, 1, ("deadline",) if k else ())
    c = cfg(divergence_counts={"deadline": (min(k, n), n)}, transfer_cost_per_contract=transfer)
    for r in scan_pair(book([], asks_a), book(bids_b, []), pair, TS, c):
        parts = [
            r.gross_edge,
            r.fee_a,
            r.fee_b,
            r.lockup_cost,
            r.resolution_haircut,
            r.transfer_cost,
        ]
        assert all(x is not None and not math.isnan(x) for x in parts)
        assert (
            abs(
                r.net_edge
                - (
                    r.gross_edge
                    - r.fee_a
                    - r.fee_b
                    - r.lockup_cost
                    - r.resolution_haircut
                    - r.transfer_cost
                )
            )
            < 1e-9
        )
        if r.gross_edge < r.fee_a + r.fee_b + r.lockup_cost:
            assert r.net_edge <= 0
        assert r.gross_edge > 0
