"""AC-W-05 (partial, M4): fee schedules, worked examples, boundary dates and properties."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmr.fees.models import FeeError, apply_rounding
from pmr.fees.table import FeeTable, check, parse, trade_fee
from tests.conftest import REPO

TABLE = FeeTable.load(REPO / "config" / "fees.yaml")
NOW = datetime(2026, 11, 20, tzinfo=UTC)


def test_shipped_schedules_pass_check() -> None:
    assert check(TABLE) == []


def test_kalshi_rounding_example_from_docs() -> None:
    fee = apply_rounding(
        "kalshi_ceil_micro_then_balance_cent",
        Decimal("0.00363825"),
        revenue=Decimal("-0.055"),
    )
    assert fee == Decimal("0.005")
    assert apply_rounding("kalshi_ceil_micro_then_balance_cent", Decimal("0.00363825")) == Decimal(
        "0.003639"
    )


def test_kalshi_general_and_multiplier() -> None:
    f = TABLE.trade_fee("kalshi", 0.5, 100, liquidity="taker", ts=NOW)
    assert f == Decimal("1.750000")
    half = TABLE.trade_fee("kalshi", 0.5, 100, liquidity="taker", ts=NOW, fee_multiplier=0.5)
    assert half == Decimal("0.875000")
    assert TABLE.trade_fee("kalshi", 0.5, 100, liquidity="maker", ts=NOW) == 0
    with pytest.raises(FeeError, match="maker coefficient"):
        TABLE.trade_fee(
            "kalshi", 0.5, 10, liquidity="maker", ts=NOW, fee_type="quadratic_with_maker_fees"
        )


def test_polymarket_categories_override_and_maker() -> None:
    assert TABLE.trade_fee(
        "polymarket", 0.5, 100, liquidity="taker", ts=NOW, category="crypto"
    ) == Decimal("1.75000")
    assert TABLE.trade_fee(
        "polymarket", 0.5, 100, liquidity="taker", ts=NOW, category="weather-climate"
    ) == Decimal("1.25000")
    assert TABLE.trade_fee("polymarket", 0.5, 100, liquidity="taker", ts=NOW, rate=0.0) == Decimal(
        "0"
    )
    assert TABLE.trade_fee("polymarket", 0.5, 100, liquidity="maker", ts=NOW) == 0
    # below 0.00001 rounds to zero
    assert TABLE.trade_fee(
        "polymarket", 0.999, 0.001, liquidity="taker", ts=NOW, category="politics"
    ) == Decimal("0")


def test_dq11_no_schedule_is_an_error_and_boundaries() -> None:
    with pytest.raises(FeeError, match="DQ-11"):
        TABLE.trade_fee("kalshi", 0.5, 1, liquidity="taker", ts=datetime(2026, 7, 6, 23, 59))
    TABLE.trade_fee("kalshi", 0.5, 1, liquidity="taker", ts=datetime(2026, 7, 7, 0, 0))
    with pytest.raises(FeeError, match="bad fill"):
        TABLE.trade_fee("kalshi", 1.5, 1, liquidity="taker", ts=NOW)
    assert trade_fee("manifold", 0.5, 10, liquidity="taker", ts=NOW) == 0


def test_check_finds_overlaps_bad_examples_and_nulls(tmp_path: Path) -> None:
    yaml_text = """
schedules:
  - {schedule_id: a, venue: kalshi, formula: coef_x_c_p_1mp, taker_coef: 0.07, maker_coef: 0,
     rounding: ceil_cent_per_order, effective_from: 2024-01-01, effective_to: null,
     source_url: x, worked_examples: [{price: 0.5, contracts: 100, fee_usd: 9.99}]}
  - {schedule_id: b, venue: kalshi, formula: coef_x_c_p_1mp, taker_coef: 0.07, maker_coef: 0,
     rounding: ceil_cent_per_order, effective_from: 2025-01-01, effective_to: null,
     source_url: x}
"""
    p = tmp_path / "fees.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    problems = check(FeeTable.load(p))
    assert any("overlaps" in x for x in problems)
    assert any("expected 9.99" in x for x in problems)
    with pytest.raises(FeeError, match="required"):
        parse(
            {
                "schedule_id": "z",
                "venue": "kalshi",
                "formula": "coef_x_c_p_1mp",
                "rounding": None,
                "effective_from": date(2024, 1, 1),
                "source_url": "u",
            }
        )
    with pytest.raises(FeeError, match="unknown rounding"):
        apply_rounding("weird", Decimal(1))


@given(
    st.decimals(min_value="0.01", max_value="0.99", places=2),
    st.integers(min_value=1, max_value=1000),
    st.integers(min_value=0, max_value=500),
    st.sampled_from(["kalshi", "polymarket"]),
)
def test_fee_symmetric_and_monotone(p: Decimal, c: int, extra: int, venue: str) -> None:
    f = TABLE.trade_fee(venue, float(p), c, liquidity="taker", ts=NOW, category="sports")
    g = TABLE.trade_fee(venue, float(1 - p), c, liquidity="taker", ts=NOW, category="sports")
    assert f == g
    assert TABLE.trade_fee(venue, 0.5, c, liquidity="taker", ts=NOW, category="sports") >= f
    assert (
        TABLE.trade_fee(venue, float(p), c + extra, liquidity="taker", ts=NOW, category="sports")
        >= f
    )
