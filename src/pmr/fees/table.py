"""Effective-dated fee lookup and ``trade_fee`` (FR-16, DQ-11) plus ``pmr fees check``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from pmr.fees.models import (
    CENT,
    FeeError,
    FeeSchedule,
    Liquidity,
    apply_rounding,
    model_fee,
)

DEFAULT_PATH = Path("config/fees.yaml")
REQUIRED = ("schedule_id", "venue", "formula", "rounding", "effective_from", "source_url")

# Project taxonomy (D-08) -> Polymarket fee category (docs fees page).
POLYMARKET_CATEGORY = {
    "politics": "politics",
    "economics": "economics",
    "finance": "finance",
    "crypto": "crypto",
    "sports": "sports",
    "weather-climate": "weather",
    "science-tech": "tech",
    "culture": "culture",
    "other": "other",
}


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def parse(raw: Mapping[str, Any]) -> FeeSchedule:
    """One YAML entry to a :class:`FeeSchedule` (raises on a missing required field)."""
    missing = [k for k in REQUIRED if raw.get(k) is None]
    if raw.get("formula") != "none" and raw.get("venue") != "polymarket":
        missing += [k for k in ("taker_coef",) if raw.get(k) is None]
    if missing:
        raise FeeError(f"{raw.get('schedule_id')}: required fields are null: {missing}")
    scope = raw.get("scope") or {}
    rates = tuple(sorted((str(k), Decimal(str(v))) for k, v in (scope.get("rates") or {}).items()))
    ef = _date(raw["effective_from"])
    assert ef is not None
    return FeeSchedule(
        schedule_id=str(raw["schedule_id"]),
        venue=str(raw["venue"]),
        formula=str(raw["formula"]),
        taker_coef=_dec(raw.get("taker_coef")) or Decimal(0),
        maker_coef=_dec(raw.get("maker_coef")),
        rounding=str(raw["rounding"]),
        effective_from=ef,
        effective_to=_date(raw.get("effective_to")),
        source_url=str(raw["source_url"]),
        scope_type=str(scope.get("type", "default")),
        rates=rates,
        maker_fee_types=tuple(raw.get("maker_fee_types") or ()),
    )


@dataclass(frozen=True)
class FeeTable:
    """All schedules, with the raw YAML kept for the worked examples."""

    schedules: tuple[FeeSchedule, ...]
    raw: tuple[Mapping[str, Any], ...]

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> FeeTable:
        """Parse ``config/fees.yaml``."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        raws = tuple(data.get("schedules") or [])
        return cls(tuple(parse(r) for r in raws), raws)

    def schedule(self, venue: str, d: date) -> FeeSchedule:
        """The schedule in force for ``venue`` on ``d`` (DQ-11: none is an error)."""
        hits = [s for s in self.schedules if s.venue == venue and s.covers(d)]
        if not hits:
            raise FeeError(f"no {venue} fee schedule covers {d.isoformat()} (DQ-11)")
        return hits[0]

    def trade_fee(
        self,
        venue: str,
        price: float,
        contracts: float,
        *,
        liquidity: Liquidity,
        ts: datetime,
        category: str | None = None,
        fee_multiplier: float | None = None,
        fee_type: str | None = None,
        rate: float | None = None,
        revenue: float | None = None,
    ) -> Decimal:
        """Fee in USD (USDC on Polymarket) for one fill.

        Args:
            price: YES or NO price actually paid, in [0, 1].
            contracts: contracts or shares.
            category: project category (Polymarket rate lookup).
            fee_multiplier, fee_type: the Kalshi series' values from ``GET /series``.
            rate: a Polymarket market's own taker rate, overriding the category rate.
            revenue: signed cash flow of the fill before fees (Kalshi balance rounding).
        """
        if not 0.0 <= price <= 1.0 or contracts < 0:
            raise FeeError(f"bad fill: price={price} contracts={contracts}")
        d = (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(UTC).date()
        s = self.schedule(venue, d)
        if s.formula == "none":
            return Decimal(0)
        p, c = Decimal(str(price)), Decimal(str(contracts))
        if venue == "kalshi":
            mult = Decimal(str(1 if fee_multiplier is None else fee_multiplier))
            if liquidity == "maker":
                if fee_type in s.maker_fee_types:
                    raise FeeError(
                        f"Kalshi maker coefficient for fee_type {fee_type!r} is not transcribed "
                        "yet (M3)"
                    )
                return Decimal(0)
            fee = model_fee(s.taker_coef * mult, c, p)
            rev = None if revenue is None else Decimal(str(revenue))
            return apply_rounding(s.rounding, fee, revenue=rev)
        if venue == "polymarket":
            if liquidity == "maker":
                return Decimal(0)
            if rate is not None:
                coef = Decimal(str(rate))
            else:
                rates = dict(s.rates)
                # A Polymarket fee category is used as is; a project category is mapped.
                cat = (
                    category
                    if category in rates
                    else POLYMARKET_CATEGORY.get(category or "other", "other")
                )
                if cat not in rates:
                    raise FeeError(f"no Polymarket rate for category {cat!r}")
                coef = rates[cat]
            return apply_rounding(s.rounding, model_fee(coef, c, p))
        return apply_rounding(s.rounding, model_fee(s.taker_coef, c, p))


@lru_cache(maxsize=4)
def default_table(path: str = str(DEFAULT_PATH)) -> FeeTable:
    """Cached table from ``config/fees.yaml``."""
    return FeeTable.load(Path(path))


def trade_fee(
    venue: str,
    price: float,
    contracts: int | float,
    *,
    liquidity: Liquidity,
    ts: datetime,
    category: str | None = None,
    series: str | None = None,
    fee_multiplier: float | None = None,
    fee_type: str | None = None,
) -> Decimal:
    """Spec section 10 entry point, using ``config/fees.yaml``."""
    _ = series  # the series' multiplier and fee type are passed in explicitly
    return default_table().trade_fee(
        venue,
        price,
        float(contracts),
        liquidity=liquidity,
        ts=ts,
        category=category,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
    )


def check(table: FeeTable) -> list[str]:
    """``pmr fees check``: overlapping schedules and worked examples; returns problems."""
    problems: list[str] = []
    by_key: dict[tuple[str, str], list[FeeSchedule]] = {}
    for s in table.schedules:
        by_key.setdefault((s.venue, s.scope_type), []).append(s)
    for key, items in by_key.items():
        items = sorted(items, key=lambda s: s.effective_from)
        for a, b in zip(items, items[1:], strict=False):
            if a.effective_to is None or a.effective_to > b.effective_from:
                problems.append(f"{key}: {a.schedule_id} overlaps {b.schedule_id}")
    for raw, s in zip(table.raw, table.schedules, strict=True):
        for ex in raw.get("worked_examples") or []:
            ts = datetime.combine(s.effective_from, datetime.min.time(), tzinfo=UTC)
            if s.venue == "kalshi" and "model_fee_usd" in ex:
                got = apply_rounding(
                    s.rounding,
                    Decimal(str(ex["model_fee_usd"])),
                    revenue=Decimal(str(ex["revenue_usd"])),
                )
                want = Decimal(str(ex["total_fee_usd"]))
            else:
                got = table.trade_fee(
                    s.venue,
                    float(ex["price"]),
                    float(ex["contracts"]),
                    liquidity=ex.get("liquidity", "taker"),
                    ts=ts,
                    category=ex.get("category"),
                ).quantize(CENT, rounding=ROUND_HALF_UP)
                want = Decimal(str(ex["fee_usd"])).quantize(CENT)
            if got != want:
                problems.append(f"{s.schedule_id}: example {ex} gives {got}, expected {want}")
    return problems
