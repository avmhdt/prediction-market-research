"""Market-making strategy and simulator for one market (FR-22, AC-W-08).

Fair value is an EMA of the mid with half-life ``ema_halflife_s`` (irregular updates:
``alpha = 1 - 2 ** (-dt / half-life)``). Quotes sit at ``fair -/+ half_spread`` shifted by the
inventory skew ``-skew_gamma x q``, rounded outward to the tick, kept inside (0, 1) and never
crossing the opposite best price (post-only). A side stops quoting when another fill could
take ``|q|`` past ``q_max`` or the capital tied up past ``capital_max`` (capital of a long is
``q x price``, of a short ``|q| x (1 - price)``). Fills follow :mod:`pmr.sim.fills`. Maker fees
come from the venue's schedule.

:class:`MMStrategy` is driven event by event: the simulator replays recorded books and trades,
and the paper trader feeds it live polls (FR-24), so both use the same code.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl

from pmr.sim.fills import EPS, Quote, passive_fill
from pmr.sim.pnl import FillRecord, MMResult, decompose

FeeFn = Callable[[int, float, float], float]  # (ts_ns, price, qty) -> USD


@dataclass(frozen=True)
class MMParams:
    """Strategy parameters (``config/pmr.yaml`` section ``mm``)."""

    half_spread: float
    skew_gamma: float
    q_max: int
    capital_max: float
    ema_halflife_s: float
    quote_size: float = 10.0
    tick: float = 0.01


def _floor_tick(x: float, tick: float) -> float:
    return math.floor(round(x / tick, 9)) * tick


def _ceil_tick(x: float, tick: float) -> float:
    return math.ceil(round(x / tick, 9)) * tick


@dataclass
class MMStrategy:
    """Event-driven market maker for one symbol."""

    params: MMParams
    fee_fn: FeeFn | None = None
    q: float = 0.0
    fair: float | None = None
    last_ts: int | None = None
    bid: Quote | None = None
    ask: Quote | None = None
    fills: list[FillRecord] = field(default_factory=list)
    fair_path: list[tuple[int, float]] = field(default_factory=list)
    mtm: float = 0.0

    # ------------------------------------------------------------------ limits

    def _room(self, side: int, price: float) -> float:
        """Largest quantity a fill on ``side`` at ``price`` may add within the limits."""
        p = self.params
        q_after_max = p.q_max - self.q if side == 1 else p.q_max + self.q
        room = max(0.0, q_after_max)
        unit = price if side == 1 else 1.0 - price
        if unit > 0:
            # capital after the fill: |q_new| x unit, for fills that extend the position
            extending = (side == 1 and self.q >= 0) or (side == -1 and self.q <= 0)
            if extending:
                room = min(room, max(0.0, p.capital_max / unit - abs(self.q)))
        return room

    # ------------------------------------------------------------------ events

    def on_book(self, ts: int, book: pl.DataFrame) -> None:
        """A full snapshot: update fair value, mark inventory, re-quote."""
        bids = book.filter(pl.col("side") == 1)
        asks = book.filter(pl.col("side") == -1)
        best_bid = float(bids["price"].max()) if bids.height else None  # type: ignore[arg-type]
        best_ask = float(asks["price"].min()) if asks.height else None  # type: ignore[arg-type]
        if best_bid is not None and best_ask is not None and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2.0
            if self.fair is None or self.last_ts is None:
                new_fair = mid
            else:
                dt = max(0.0, (ts - self.last_ts) / 1e9)
                alpha = 1.0 - 2.0 ** (-dt / self.params.ema_halflife_s)
                new_fair = self.fair + alpha * (mid - self.fair)
            if self.fair is not None:
                self.mtm += self.q * (new_fair - self.fair)
            self.fair = new_fair
            self.last_ts = ts
            self.fair_path.append((ts, new_fair))
        if self.fair is None:
            return
        self._requote(ts, book, best_bid, best_ask)

    def _requote(
        self, ts: int, book: pl.DataFrame, best_bid: float | None, best_ask: float | None
    ) -> None:
        p = self.params
        assert self.fair is not None
        center = self.fair - p.skew_gamma * self.q
        bid_px = min(max(_floor_tick(center - p.half_spread, p.tick), p.tick), 1 - p.tick)
        ask_px = max(min(_ceil_tick(center + p.half_spread, p.tick), 1 - p.tick), p.tick)
        if best_ask is not None and bid_px >= best_ask - EPS:
            bid_px = round(best_ask - p.tick, 10)
        if best_bid is not None and ask_px <= best_bid + EPS:
            ask_px = round(best_bid + p.tick, 10)
        self.bid = self._place(ts, book, 1, bid_px, self.bid)
        self.ask = self._place(ts, book, -1, ask_px, self.ask)

    def _place(
        self, ts: int, book: pl.DataFrame, side: int, price: float, old: Quote | None
    ) -> Quote | None:
        if not 0.0 < price < 1.0:
            return None
        size = min(self.params.quote_size, self._room(side, price))
        if size <= EPS:
            return None
        if old is not None and abs(old.price - price) <= EPS:
            old.size = size  # same price: keep the queue position
            return old
        lvl = book.filter((pl.col("side") == side) & ((pl.col("price") - price).abs() <= EPS))
        ahead = float(lvl["qty"].sum()) if lvl.height else 0.0
        return Quote(side=side, price=price, size=size, queue_ahead=ahead, posted_ts=ts)

    def on_trade(self, ts: int, price: float, qty: float, side: int) -> list[FillRecord]:
        """A venue trade print: check both resting quotes for passive fills."""
        out: list[FillRecord] = []
        for quote in (self.bid, self.ask):
            if quote is None or self.fair is None:
                continue
            room = self._room(quote.side, quote.price)
            if room <= EPS:
                continue
            quote.size = min(quote.size, room)
            got = passive_fill(quote, price, qty, side)
            if got <= EPS:
                continue
            fee = self.fee_fn(ts, quote.price, got) if self.fee_fn else 0.0
            rec = FillRecord(
                ts=ts, side=quote.side, price=quote.price, qty=got, fair=self.fair, fee=fee
            )
            self.q += quote.side * got
            self.fills.append(rec)
            out.append(rec)
        return out

    def result(self, settlement: float | None = None) -> MMResult:
        """P&L decomposition up to now (settled at ``settlement`` when given)."""
        return decompose(self.fills, self.fair_path, self.mtm, self.q, settlement)


def simulate_mm(
    books: pl.DataFrame,
    trades: pl.DataFrame,
    params: MMParams,
    *,
    venue: str,
    symbol: str,
    fee_fn: FeeFn | None = None,
    settlement: float | None = None,
) -> MMResult:
    """Replay recorded ``book_l2`` snapshots and ``trades`` for one symbol.

    Events are processed in time order; at equal timestamps the book comes first.
    """
    b = books.filter((pl.col("venue") == venue) & (pl.col("symbol") == symbol))
    t = trades.filter((pl.col("venue") == venue) & (pl.col("symbol") == symbol)).sort(
        ["ts_exchange", "trade_id"]
    )
    snaps = b.group_by("seq", maintain_order=True).agg(pl.col("ts_recv").max()).sort("seq")
    trade_rows = list(t.iter_rows(named=True))
    events: list[tuple[int, int, int]] = [
        (int(ts), 0, int(seq)) for seq, ts in zip(snaps["seq"], snaps["ts_recv"], strict=True)
    ]
    events += [(int(r["ts_exchange"]), 1, i) for i, r in enumerate(trade_rows)]
    events.sort(key=lambda e: (e[0], e[1]))
    strat = MMStrategy(params, fee_fn)
    for ts, kind, ref in events:
        if kind == 0:
            strat.on_book(ts, b.filter(pl.col("seq") == ref))
        else:
            r = trade_rows[ref]
            strat.on_trade(ts, float(r["price"]), float(r["qty"]), int(r["side"]))
    return strat.result(settlement)
