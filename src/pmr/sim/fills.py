"""Passive-fill rules shared by the simulator and the paper trader (FR-22, R-09).

A resting quote fills only when a later trade

* prints *through* it (a trade below our bid, or above our ask): fills up to the trade size;
* prints *at* it from the other side's initiative: the trade first uses up the volume that was
  ahead of us in the queue (the book size at our price when we posted), then fills us.

Trades whose initiator is unknown (side 0) never fill at the quote, only through it. Taker
fills (arb and FLB orders) take only the depth seen at the poll.
"""

from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-12


@dataclass
class Quote:
    """A resting paper limit order. ``side`` +1 is a bid (we buy YES), -1 an ask (we sell)."""

    side: int
    price: float
    size: float
    queue_ahead: float
    posted_ts: int


def passive_fill(quote: Quote, trade_price: float, trade_qty: float, trade_side: int) -> float:
    """Quantity of ``quote`` filled by one trade; updates ``queue_ahead`` and ``size`` in place.

    ``trade_side`` is the initiator: +1 a YES buyer lifted an ask, -1 a YES seller hit a bid.
    """
    if quote.size <= EPS or trade_qty <= EPS:
        return 0.0
    if quote.side == 1:
        through = trade_price < quote.price - EPS
        at = abs(trade_price - quote.price) <= EPS and trade_side == -1
    else:
        through = trade_price > quote.price + EPS
        at = abs(trade_price - quote.price) <= EPS and trade_side == 1
    if through:
        qty = min(quote.size, trade_qty)
    elif at:
        used = min(quote.queue_ahead, trade_qty)
        quote.queue_ahead -= used
        qty = min(quote.size, trade_qty - used)
    else:
        return 0.0
    quote.size -= qty
    return qty


def taker_fill(levels: list[tuple[float, float]], max_qty: float) -> list[tuple[float, float]]:
    """Walk ``(price, qty)`` levels (best first) for up to ``max_qty``; returns the fills."""
    out: list[tuple[float, float]] = []
    left = max_qty
    for price, qty in levels:
        if left <= EPS:
            break
        take = min(qty, left)
        if take > EPS:
            out.append((price, take))
            left -= take
    return out
