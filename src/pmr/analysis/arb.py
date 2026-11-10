"""Arbitrage scanner net of fees, lockup and resolution risk (FR-20, AC-W-02, AC-H-05).

Books are canonical ``book_l2`` rows for one symbol and one snapshot (columns ``side``,
``price``, ``qty``; side +1 YES bid, -1 YES ask). Buying YES walks the YES asks; buying NO
walks the YES bids as NO asks at ``1 - bid``.

Every opportunity row is per contract (USD per contract, except ``size`` in contracts and
``days_to_resolution``) and satisfies, to 1e-9::

    net_edge = gross_edge - fee_a - fee_b - lockup_cost - resolution_haircut - transfer_cost

* ``cost``: average price paid for the whole hedge per contract; ``gross_edge = payoff - cost``.
* Fees: taker fees on every fill, each leg separately (Decimal, venue rounding).
* ``lockup_cost``: :func:`pmr.analysis.lockup.lockup_cost` on each leg's capital, to the later
  expected resolution plus settlement lags; days are floored at 1 for annualising.
* ``resolution_haircut``: for each hand-confirmed material rule difference type t,
  ``P(divergence | t) x loss_given_divergence`` with ``P`` the posterior mean of a Beta prior
  updated with the mismatch catalogue's counts (M6; zero counts until then).
* ``transfer_cost``: a configurable per-contract cost of moving capital between venues.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import polars as pl

from pmr.analysis.lockup import days_to_resolution, lockup_cost
from pmr.fees.table import FeeTable

Level = tuple[float, float]  # (price paid per contract, quantity available)


@dataclass(frozen=True)
class LegInfo:
    """What the fee and lockup calculations need about one market."""

    venue: str
    symbol: str
    expected_resolution_ns: int
    category: str | None = None
    fee_multiplier: float | None = None
    fee_type: str | None = None
    fee_rate: float | None = None


@dataclass(frozen=True)
class ContractPair:
    """A confirmed cross-venue pair (``config/contract_map.yaml``)."""

    pair_id: str
    a: LegInfo
    b: LegInfo
    orientation: int  # +1: YES_a == YES_b; -1: YES_a == NO_b
    material_diffs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArbConfig:
    """Scanner parameters (``config/pmr.yaml`` section ``arb``)."""

    fees: FeeTable
    r_annual: float
    collateral_yield: Mapping[str, float] = field(default_factory=dict)
    settlement_lag_hours: Mapping[str, float] = field(default_factory=dict)
    transfer_cost_per_contract: float = 0.0
    divergence_prior: tuple[float, float] = (1.0, 1.0)
    divergence_counts: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    loss_given_divergence: float = 1.0
    max_rows_per_scan: int = 50


@dataclass(frozen=True)
class ArbOpportunity:
    """One executable size on one snapshot (per-contract USD values)."""

    ts: int
    pair_id: str
    kind: str
    direction: str
    size: float
    cost: float
    gross_edge: float
    fee_a: float
    fee_b: float
    lockup_cost: float
    resolution_haircut: float
    transfer_cost: float
    net_edge: float
    days_to_resolution: float
    net_annualized: float


def buy_yes_levels(book: pl.DataFrame) -> list[Level]:
    """YES asks, cheapest first."""
    asks = book.filter(pl.col("side") == -1).sort("price")
    return [(float(p), float(q)) for p, q in zip(asks["price"], asks["qty"], strict=True) if q > 0]


def buy_no_levels(book: pl.DataFrame) -> list[Level]:
    """NO asks derived from YES bids (``1 - bid``), cheapest first."""
    bids = book.filter(pl.col("side") == 1).sort("price", descending=True)
    return [
        (1.0 - float(p), float(q)) for p, q in zip(bids["price"], bids["qty"], strict=True) if q > 0
    ]


def haircut(diffs: Sequence[str], cfg: ArbConfig) -> float:
    """``sum_t P(divergence | t) x LGD`` with Beta posterior means (FR-20)."""
    a, b = cfg.divergence_prior
    total = 0.0
    for t in diffs:
        k, n = cfg.divergence_counts.get(t, (0, 0))
        total += (k + a) / (n + a + b) * cfg.loss_given_divergence
    return total


def _fee(cfg: ArbConfig, leg: LegInfo, price: float, qty: float, ts: int) -> float:
    when = datetime.fromtimestamp(ts / 1e9, tz=UTC)
    return float(
        cfg.fees.trade_fee(
            leg.venue,
            min(max(price, 0.0), 1.0),
            qty,
            liquidity="taker",
            ts=when,
            category=leg.category,
            fee_multiplier=leg.fee_multiplier,
            fee_type=leg.fee_type,
            rate=leg.fee_rate,
            revenue=-price * qty if leg.venue == "kalshi" else None,
        )
    )


def walk(
    legs: Sequence[tuple[LegInfo, list[Level]]],
    *,
    ts: int,
    payoff: float,
    cfg: ArbConfig,
    pair_id: str,
    kind: str,
    direction: str,
    diffs: Sequence[str] = (),
    cross_venue: bool = False,
) -> list[ArbOpportunity]:
    """Buy one unit of every leg together, level by level; one row per depth breakpoint.

    ``fee_a`` is the first leg's fees and ``fee_b`` all other legs' fees (multi-outcome sets
    put every leg after the first in ``fee_b``).
    """
    if any(not lv for _, lv in legs):
        return []
    idx = [0] * len(legs)
    left = [lv[0][1] for _, lv in legs]
    size = 0.0
    spent = [0.0] * len(legs)
    fees = [0.0] * len(legs)
    out: list[ArbOpportunity] = []
    res = [leg.expected_resolution_ns for leg, _ in legs]
    lags = [cfg.settlement_lag_hours.get(leg.venue, 0.0) for leg, _ in legs]
    i_last = max(range(len(legs)), key=lambda i: res[i] + lags[i] * 3600e9)
    days = days_to_resolution(ts, res[i_last], lag_a_hours=lags[i_last])
    hc = haircut(diffs, cfg)
    transfer = cfg.transfer_cost_per_contract if cross_venue else 0.0
    while len(out) < cfg.max_rows_per_scan:
        step = min(left)
        if step <= 0:
            break
        unit = sum(lv[idx[i]][0] for i, (_, lv) in enumerate(legs))
        if payoff - unit <= 0:  # walking deeper only gets more expensive
            break
        for i, (leg, lv) in enumerate(legs):
            price = lv[idx[i]][0]
            spent[i] += price * step
            fees[i] += _fee(cfg, leg, price, step, ts)
            left[i] -= step
        size += step
        cost = sum(spent) / size
        gross = payoff - cost
        fee_a = fees[0] / size
        fee_b = sum(fees[1:]) / size
        lock = sum(
            lockup_cost(
                spent[i] / size, days, cfg.r_annual, cfg.collateral_yield.get(leg.venue, 0.0)
            )
            for i, (leg, _) in enumerate(legs)
        )
        net = gross - fee_a - fee_b - lock - hc - transfer
        ann = net / cost * 365.0 / max(days, 1.0) if cost > 0 else math.nan
        out.append(
            ArbOpportunity(
                ts=ts,
                pair_id=pair_id,
                kind=kind,
                direction=direction,
                size=size,
                cost=cost,
                gross_edge=gross,
                fee_a=fee_a,
                fee_b=fee_b,
                lockup_cost=lock,
                resolution_haircut=hc,
                transfer_cost=transfer,
                net_edge=net,
                days_to_resolution=days,
                net_annualized=ann,
            )
        )
        exhausted = False
        for i, (_, lv) in enumerate(legs):
            if left[i] <= 1e-12:
                idx[i] += 1
                if idx[i] >= len(lv):
                    exhausted = True
                else:
                    left[i] = lv[idx[i]][1]
        if exhausted:
            break
    return out


def scan_pair(
    book_a: pl.DataFrame, book_b: pl.DataFrame, pair: ContractPair, ts: int, cfg: ArbConfig
) -> list[ArbOpportunity]:
    """Cross-venue hedges for one confirmed pair, both directions, orientation-aware."""
    if pair.orientation == 1:
        plans = [
            ("yes_a_no_b", buy_yes_levels(book_a), buy_no_levels(book_b)),
            ("no_a_yes_b", buy_no_levels(book_a), buy_yes_levels(book_b)),
        ]
    else:
        plans = [
            ("yes_a_yes_b", buy_yes_levels(book_a), buy_yes_levels(book_b)),
            ("no_a_no_b", buy_no_levels(book_a), buy_no_levels(book_b)),
        ]
    out: list[ArbOpportunity] = []
    for direction, la, lb in plans:
        out += walk(
            [(pair.a, la), (pair.b, lb)],
            ts=ts,
            payoff=1.0,
            cfg=cfg,
            pair_id=pair.pair_id,
            kind="cross_venue",
            direction=direction,
            diffs=pair.material_diffs,
            cross_venue=True,
        )
    return out


def scan_yes_no(
    leg: LegInfo, yes_levels: list[Level], no_levels: list[Level], ts: int, cfg: ArbConfig
) -> list[ArbOpportunity]:
    """Intra-venue YES + NO bought together for less than 1 (``kind = yes_no``)."""
    return walk(
        [(leg, yes_levels), (leg, no_levels)],
        ts=ts,
        payoff=1.0,
        cfg=cfg,
        pair_id=f"{leg.venue}:{leg.symbol}",
        kind="yes_no",
        direction="buy_yes_no",
    )


def scan_multi_outcome(
    set_id: str, legs: Sequence[tuple[LegInfo, pl.DataFrame]], ts: int, cfg: ArbConfig
) -> list[ArbOpportunity]:
    """Mutually exclusive, exhaustive outcomes: buy every YES (payoff 1) or every NO
    (payoff n - 1) when the set is mispriced (``kind = multi_outcome``)."""
    n = len(legs)
    if n < 2:
        return []
    yes = [(info, buy_yes_levels(book)) for info, book in legs]
    no = [(info, buy_no_levels(book)) for info, book in legs]
    return walk(
        yes,
        ts=ts,
        payoff=1.0,
        cfg=cfg,
        pair_id=set_id,
        kind="multi_outcome",
        direction="buy_all_yes",
    ) + walk(
        no,
        ts=ts,
        payoff=float(n - 1),
        cfg=cfg,
        pair_id=set_id,
        kind="multi_outcome",
        direction="buy_all_no",
    )


def to_frame(rows: Sequence[ArbOpportunity]) -> pl.DataFrame:
    """Rows to a DataFrame with the ``arb_opportunities`` columns."""
    cols = list(ArbOpportunity.__dataclass_fields__)
    return pl.DataFrame([[getattr(r, c) for c in cols] for r in rows], schema=cols, orient="row")
