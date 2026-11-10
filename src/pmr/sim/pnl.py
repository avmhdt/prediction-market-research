"""P&L decomposition for the market maker (FR-22, AC-W-08).

With ``f`` the fair value at each fill, ``V`` the final value (settlement or last fair)::

    total = spread_capture + inventory_mtm + settlement - fees

* ``spread_capture = sum(side x (f - price) x qty)``: edge against fair value at the fill.
* ``inventory_mtm = sum(q x change in fair)``: inventory marked between fair-value updates.
* ``settlement = q_end x (V - last fair)``.
* ``fees``: maker fees paid.

The identity holds exactly (to floating error) because ``cash + q_end x V`` equals the sum.
Markouts at +5 min and +1 h (``side x (f(t + h) - f(t)) x qty``) measure adverse selection; they
are a diagnostic part of ``inventory_mtm``, not an extra term.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

MARKOUTS_S = (300, 3600)


@dataclass(frozen=True)
class FillRecord:
    """One paper or simulated fill."""

    ts: int
    side: int
    price: float
    qty: float
    fair: float
    fee: float = 0.0


@dataclass(frozen=True)
class MMResult:
    """Fills, final inventory and the P&L components (USD)."""

    fills: tuple[FillRecord, ...]
    q_end: float
    cash: float
    spread_capture: float
    inventory_mtm: float
    settlement: float
    fees: float
    total: float
    markouts: dict[int, float] = field(default_factory=dict)
    markout_n: dict[int, int] = field(default_factory=dict)


def fair_at(path: list[tuple[int, float]], ts: int) -> float | None:
    """Fair value in force at ``ts`` (last update at or before it)."""
    times = [t for t, _ in path]
    i = bisect.bisect_right(times, ts) - 1
    return path[i][1] if i >= 0 else None


def decompose(
    fills: list[FillRecord],
    fair_path: list[tuple[int, float]],
    mtm: float,
    q_end: float,
    settlement_value: float | None,
) -> MMResult:
    """Build an :class:`MMResult` from a strategy's state."""
    cash = -sum(f.side * f.price * f.qty for f in fills)
    fees = sum(f.fee for f in fills)
    spread = sum(f.side * (f.fair - f.price) * f.qty for f in fills)
    last_fair = fair_path[-1][1] if fair_path else 0.0
    final = last_fair if settlement_value is None else settlement_value
    settle = q_end * (final - last_fair)
    total = cash + q_end * final - fees
    end_ts = fair_path[-1][0] if fair_path else 0
    marks: dict[int, float] = {}
    counts: dict[int, int] = {}
    for h in MARKOUTS_S:
        tot, n = 0.0, 0
        for f in fills:
            if f.ts + h * 10**9 > end_ts:
                continue
            later = fair_at(fair_path, f.ts + h * 10**9)
            if later is None:
                continue
            tot += f.side * (later - f.fair) * f.qty
            n += 1
        marks[h], counts[h] = tot, n
    return MMResult(
        fills=tuple(fills),
        q_end=q_end,
        cash=cash,
        spread_capture=spread,
        inventory_mtm=mtm,
        settlement=settle,
        fees=fees,
        total=total,
        markouts=marks,
        markout_n=counts,
    )
