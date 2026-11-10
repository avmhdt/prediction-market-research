"""Fee functions (FR-16, NFR-05). All arithmetic in :class:`decimal.Decimal`.

``fee = coef x multiplier x C x P x (1 - P)`` with the venue's rounding:

* ``kalshi_ceil_micro_then_balance_cent`` (docs.kalshi.com, fee rounding): the trade fee is
  the model fee rounded up to $0.000001; the balance change ``revenue - trade fee`` is then
  floored to the member's balance precision ($0.01 for non-direct members), and the difference
  is a rounding fee. The per-order rebate accumulator is ignored (conservative for one-fill
  paper orders).
* ``round_5dp_min_0_00001`` (Polymarket): rounded to 5 decimals, so anything below 0.00001 is
  zero. The page does not say up or nearest; half-up is used.
* ``none``: zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

Liquidity = Literal["maker", "taker"]
MICRO = Decimal("0.000001")
CENT = Decimal("0.01")
FIVE_DP = Decimal("0.00001")


class FeeError(ValueError):
    """No schedule covers the fill, or a needed coefficient is unknown (DQ-11)."""


@dataclass(frozen=True)
class FeeSchedule:
    """One effective-dated schedule (``config/fees.yaml``)."""

    schedule_id: str
    venue: str
    formula: str
    taker_coef: Decimal
    maker_coef: Decimal | None
    rounding: str
    effective_from: date
    effective_to: date | None
    source_url: str
    scope_type: str = "default"
    rates: tuple[tuple[str, Decimal], ...] = ()
    maker_fee_types: tuple[str, ...] = ()

    def covers(self, d: date) -> bool:
        """``effective_from <= d < effective_to`` (inclusive / exclusive)."""
        return self.effective_from <= d and (self.effective_to is None or d < self.effective_to)


def model_fee(coef: Decimal, contracts: Decimal, price: Decimal) -> Decimal:
    """Unrounded ``coef x C x P x (1 - P)`` in USD."""
    return coef * contracts * price * (Decimal(1) - price)


def apply_rounding(
    rule: str, fee: Decimal, *, revenue: Decimal | None = None, precision: Decimal = CENT
) -> Decimal:
    """Round a model fee by the venue's rule (see module docstring)."""
    if rule == "none":
        return Decimal(0)
    if rule == "round_5dp_min_0_00001":
        return fee.quantize(FIVE_DP, rounding=ROUND_HALF_UP)
    if rule == "kalshi_ceil_micro_then_balance_cent":
        trade_fee = fee.quantize(MICRO, rounding=ROUND_CEILING)
        if revenue is None:
            return trade_fee
        change = revenue - trade_fee
        aligned = change.quantize(precision, rounding=ROUND_FLOOR)
        return revenue - aligned
    if rule == "ceil_cent_per_order":
        return fee.quantize(CENT, rounding=ROUND_CEILING)
    raise FeeError(f"unknown rounding rule {rule!r}")
