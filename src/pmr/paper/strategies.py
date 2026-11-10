"""Paper strategies (D-09): arb monitor, market-making quotes, favorite-longshot tilt.

Every strategy sees one poll cycle at a time (:class:`Cycle`) and returns events for the
ledger. They reuse the simulator's code: :class:`pmr.sim.mm.MMStrategy` for quotes and passive
fills, :mod:`pmr.analysis.arb` for executable edges, :func:`pmr.sim.fills.taker_fill` for
taker fills at the depth seen at the poll (R-09). Positions are kept in YES units: buying NO
at ``p`` is recorded as selling YES at ``1 - p``, which has the same payoff.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl

from pmr.analysis.arb import (
    ArbConfig,
    LegInfo,
    buy_no_levels,
    buy_yes_levels,
    scan_multi_outcome,
    scan_yes_no,
)
from pmr.fees.models import FeeError
from pmr.fees.table import FeeTable
from pmr.paper.config import ArbCfg, FLBCfg, MMCfg
from pmr.paper.market_set import MarketMeta
from pmr.sim.fills import taker_fill
from pmr.sim.mm import MMParams, MMStrategy

Event = dict[str, Any]


@dataclass(frozen=True)
class TradePrint:
    """A venue trade from the recorder (YES orientation)."""

    venue: str
    symbol: str
    ts: int
    price: float
    qty: float
    side: int


@dataclass
class Cycle:
    """One poll: books per ``venue:symbol`` (YES token) and NO-token books, new trades."""

    ts: int
    books: dict[str, pl.DataFrame]
    no_books: dict[str, pl.DataFrame]
    trades: list[TradePrint]
    meta: Mapping[str, MarketMeta]


def leg_info(m: MarketMeta) -> LegInfo:
    """Fee and lockup context for the arb scanner."""
    return LegInfo(
        venue=m.venue,
        symbol=m.symbol,
        expected_resolution_ns=m.close_ts,
        category=m.category,
        fee_multiplier=m.fee_multiplier,
        fee_type=m.fee_type,
        fee_rate=m.fee_rate,
    )


def taker_fee(fees: FeeTable, m: MarketMeta, price: float, qty: float, ts: int) -> float | None:
    """Taker fee for a paper fill, or None when the schedule cannot price it."""
    try:
        return float(
            fees.trade_fee(
                m.venue,
                min(max(price, 0.0), 1.0),
                qty,
                liquidity="taker",
                ts=datetime.fromtimestamp(ts / 1e9, tz=UTC),
                category=m.category,
                fee_multiplier=m.fee_multiplier,
                fee_type=m.fee_type,
                rate=m.fee_rate,
                revenue=-price * qty if m.venue == "kalshi" else None,
            )
        )
    except FeeError:
        return None


def _fill(
    strategy: str,
    m: MarketMeta,
    ts: int,
    side: int,
    price: float,
    qty: float,
    liquidity: str,
    fee: float | None,
    reason: str,
) -> Event:
    """A paper fill in YES units (side +1 buys YES, -1 sells YES)."""
    return {
        "type": "fill",
        "ts": ts,
        "strategy": strategy,
        "venue": m.venue,
        "symbol": m.symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "liquidity": liquidity,
        "fee": fee,
        "reason": reason,
    }


def _intent(
    strategy: str,
    m: MarketMeta,
    ts: int,
    side: int,
    price: float,
    qty: float,
    liquidity: str,
    reason: str,
) -> Event:
    return {
        "type": "order_intent",
        "ts": ts,
        "strategy": strategy,
        "venue": m.venue,
        "symbol": m.symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "liquidity": liquidity,
        "reason": reason,
    }


@dataclass
class ArbMonitor:
    """Intra-venue YES + NO and multi-outcome sets (cross-venue pairs once M3 confirms them).

    Takes an opportunity when its net edge is at least ``min_net_edge``, at the walked size,
    capped at ``max_notional_per_trade`` USD; the hedge is held to resolution.
    """

    cfg: ArbCfg
    arb_cfg: ArbConfig
    name: str = "arb_monitor"

    def on_cycle(self, c: Cycle) -> list[Event]:
        out: list[Event] = []
        for key, m in c.meta.items():
            if m.venue != "polymarket" or key not in c.books or key not in c.no_books:
                continue
            yes = buy_yes_levels(c.books[key])
            no_token_asks = buy_yes_levels(c.no_books[key])  # the NO token's own asks
            opps = scan_yes_no(leg_info(m), yes, no_token_asks, c.ts, self.arb_cfg)
            out += self._take(c, opps, [(m, yes, 1), (m, no_token_asks, -1)])
        groups: dict[str, list[MarketMeta]] = defaultdict(list)
        for m in c.meta.values():
            if m.mutually_exclusive and m.event_id:
                groups[f"{m.venue}:{m.event_id}"].append(m)
        for gid, ms in sorted(groups.items()):
            # Only a complete set of outcomes pays exactly 1: every open outcome of the event
            # must be in the market set and have a book this cycle.
            size = ms[0].event_size
            if len(ms) < 2 or size != len(ms) or any(m.key not in c.books for m in ms):
                continue
            legs = [(leg_info(m), c.books[m.key]) for m in sorted(ms, key=lambda x: x.symbol)]
            for o in scan_multi_outcome(gid, legs, c.ts, self.arb_cfg):
                if o.net_edge < self.cfg.min_net_edge:
                    continue
                buy_yes = o.direction == "buy_all_yes"
                plan = [
                    (
                        m,
                        buy_yes_levels(c.books[m.key])
                        if buy_yes
                        else buy_no_levels(c.books[m.key]),
                        1 if buy_yes else -1,
                    )
                    for m in sorted(ms, key=lambda x: x.symbol)
                ]
                out += self._take(c, [o], plan)
                break
        return out

    def _take(
        self, c: Cycle, opps: list[Any], plan: list[tuple[MarketMeta, Any, int]]
    ) -> list[Event]:
        good = [o for o in opps if o.net_edge >= self.cfg.min_net_edge]
        if not good:
            return [
                {
                    "type": "opportunity_seen",
                    "ts": c.ts,
                    "strategy": self.name,
                    "pair_id": o.pair_id,
                    "kind": o.kind,
                    "gross_edge": o.gross_edge,
                    "net_edge": o.net_edge,
                    "size": o.size,
                }
                for o in opps[:1]
            ]
        best = max(good, key=lambda o: o.net_edge * o.size)
        size = min(best.size, self.cfg.max_notional_per_trade / max(best.cost, 1e-9))
        out: list[Event] = [
            {
                "type": "opportunity",
                "ts": c.ts,
                "strategy": self.name,
                "pair_id": best.pair_id,
                "kind": best.kind,
                "direction": best.direction,
                "size": size,
                "gross_edge": best.gross_edge,
                "net_edge": best.net_edge,
            }
        ]
        for m, levels, how in plan:
            for price, qty in taker_fill(levels, size):
                # how = +1 buys YES at price; how = -1 buys NO at price = sells YES at 1 - price
                side, px = (1, price) if how == 1 else (-1, 1.0 - price)
                out.append(_intent(self.name, m, c.ts, side, px, qty, "taker", best.kind))
                out.append(
                    _fill(
                        self.name,
                        m,
                        c.ts,
                        side,
                        px,
                        qty,
                        "taker",
                        taker_fee(self.arb_cfg.fees, m, price, qty, c.ts),
                        best.kind,
                    )
                )
        return out


@dataclass
class MMQuotes:
    """One :class:`MMStrategy` per market, total capital shared (``capital_max``)."""

    cfg: MMCfg
    base: MMParams
    fees: FeeTable
    name: str = "mm_quotes"
    makers: dict[str, MMStrategy] = field(default_factory=dict)

    def _maker(self, m: MarketMeta, n_markets: int) -> MMStrategy | None:
        if m.maker_fee_unknown:
            return None  # Kalshi maker coefficient not transcribed yet (M3)
        if m.key not in self.makers:
            # capital_max is per strategy: split evenly over the markets in the set
            per_market_cap = self.cfg.capital_max / max(1, n_markets)
            params = MMParams(
                half_spread=self.base.half_spread,
                skew_gamma=self.base.skew_gamma,
                q_max=self.cfg.q_max,
                capital_max=per_market_cap,
                ema_halflife_s=self.base.ema_halflife_s,
                quote_size=self.base.quote_size,
                tick=m.tick,
            )
            fee_fn: Callable[[int, float, float], float] = lambda ts, px, q: 0.0  # noqa: E731
            self.makers[m.key] = MMStrategy(params, fee_fn)
        return self.makers[m.key]

    def on_cycle(self, c: Cycle) -> list[Event]:
        out: list[Event] = []
        by_key: dict[str, list[TradePrint]] = defaultdict(list)
        for t in c.trades:
            by_key[f"{t.venue}:{t.symbol}"].append(t)
        for key, m in sorted(c.meta.items()):
            mk = self._maker(m, len(c.meta))
            if mk is None:
                continue
            for t in sorted(by_key.get(key, []), key=lambda t: t.ts):
                for f in mk.on_trade(t.ts, t.price, t.qty, t.side):
                    out.append(
                        _fill(self.name, m, f.ts, f.side, f.price, f.qty, "maker", f.fee, "passive")
                    )
            if key in c.books:
                mk.on_book(c.ts, c.books[key])
                for q in (mk.bid, mk.ask):
                    if q is not None:
                        out.append(
                            _intent(self.name, m, c.ts, q.side, q.price, q.size, "maker", "quote")
                        )
        return out


@dataclass
class FLBTilt:
    """Buy the favourite (YES or NO) once per market when its ask is in a bucket."""

    cfg: FLBCfg
    fees: FeeTable
    name: str = "flb_tilt"
    done: set[str] = field(default_factory=set)

    def on_cycle(self, c: Cycle) -> list[Event]:
        out: list[Event] = []
        for key, m in sorted(c.meta.items()):
            if key in self.done or key not in c.books:
                continue
            book = c.books[key]
            for how, levels in ((1, buy_yes_levels(book)), (-1, buy_no_levels(book))):
                if not levels:
                    continue
                best = levels[0][0]
                if not any(lo <= best <= hi for lo, hi in self.cfg.buckets):
                    continue
                qty = self.cfg.notional_per_market / best
                fills = taker_fill([lv for lv in levels if lv[0] == best], qty)
                for price, q in fills:
                    side, px = (1, price) if how == 1 else (-1, 1.0 - price)
                    out.append(_intent(self.name, m, c.ts, side, px, q, "taker", "flb_bucket"))
                    out.append(
                        _fill(
                            self.name,
                            m,
                            c.ts,
                            side,
                            px,
                            q,
                            "taker",
                            taker_fee(self.fees, m, price, q, c.ts),
                            "flb_bucket",
                        )
                    )
                if fills:
                    self.done.add(key)
                    break
        return out
