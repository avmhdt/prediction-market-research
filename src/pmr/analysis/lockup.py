"""Capital lockup cost (FR-19, D-10).

``lockup_cost(capital, days, r, y) = capital x ((1 + r - y) ** (days / 365) - 1)``

``r`` is the annual risk-free rate as a decimal (FRED DTB3 percent / 100 on the observation
date) and ``y`` the collateral yield the venue pays on balances (0 unless the venue's current
official terms state interest, D-10). Days run to the later of the two expected resolution
times plus each venue's settlement lag.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import date

NS_PER_DAY = 86_400 * 10**9


def lockup_cost(
    capital: float, days: float, r_annual: float, collateral_yield: float = 0.0
) -> float:
    """Opportunity cost in USD of locking ``capital`` USD for ``days`` days."""
    if days <= 0 or capital == 0:
        return 0.0
    growth: float = (1.0 + r_annual - collateral_yield) ** (days / 365.0)
    return capital * (growth - 1.0)


def days_to_resolution(
    ts_ns: int,
    resolution_a_ns: int,
    resolution_b_ns: int | None = None,
    *,
    lag_a_hours: float = 0.0,
    lag_b_hours: float = 0.0,
) -> float:
    """Days from ``ts`` to the later of the legs' expected resolution plus settlement lag."""
    a = resolution_a_ns + int(lag_a_hours * 3600 * 10**9)
    b = a if resolution_b_ns is None else resolution_b_ns + int(lag_b_hours * 3600 * 10**9)
    return max(0.0, (max(a, b) - ts_ns) / NS_PER_DAY)


class RateCurve:
    """DTB3 lookup: the last published rate on or before a date, as a decimal."""

    def __init__(self, dates: Sequence[date], rates_pct: Sequence[float]) -> None:
        pairs = sorted(zip(dates, rates_pct, strict=True))
        self._dates = [d for d, _ in pairs]
        self._rates = [r / 100.0 for _, r in pairs]

    def at(self, d: date) -> float:
        """Annual decimal rate in force on ``d`` (raises before the first observation)."""
        i = bisect.bisect_right(self._dates, d) - 1
        if i < 0:
            raise ValueError(f"no DTB3 observation on or before {d}")
        return self._rates[i]
