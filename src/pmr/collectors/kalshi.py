"""Kalshi Trade API v2 collector (FR-01). Public endpoints, no key by default.

Routing by ``GET /historical/cutoff`` (docs.kalshi.com, historical data guide): settled
markets and their candlesticks older than ``market_settled_ts``, and trades older than
``trades_created_ts``, exist only under ``/historical/*``. Live and historical copies of the
same trade are de-duplicated in the warehouse, keeping the live record (DQ-08).

Storage gate: Kalshi's Developer Agreement and Data Terms restrict storing API data
(docs/venue_notes.md). Until the owner rules, ``venues.kalshi.storage_allowed`` is false and
every ingest that would write Kalshi data refuses to start.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pmr.collectors.base import (
    CollectContext,
    CollectorError,
    IngestStats,
    Stream,
    cursor_pages,
    done_set,
    mark_done,
    to_unix,
)
from pmr.settings import PmrConfig

VENUE = "kalshi"
WHAT = ("series", "events", "markets", "historical", "trades", "candles", "books")
CANDLE_PERIOD_MIN = 60
MAX_CANDLES_PER_CALL = 4000  # hours per request window (documented batch cap is 10,000)


def _cursor(data: Any) -> str | None:
    if isinstance(data, dict):
        c = data.get("cursor")
        return str(c) if c else None
    return None


def _count(field: str) -> Any:
    def f(data: Any) -> int:
        if isinstance(data, dict) and isinstance(data.get(field), list):
            return len(data[field])
        return 0

    return f


def _iso_to_unix(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def require_storage_allowed(cfg: PmrConfig) -> None:
    """Raise unless the owner has allowed storing Kalshi data."""
    if not cfg.venues.kalshi.storage_allowed:
        raise CollectorError(
            "Kalshi ingest is disabled: storing Kalshi API data waits for the owner's ruling "
            "on Kalshi's Developer Agreement and Data Terms (docs/venue_notes.md). "
            "Set venues.kalshi.storage_allowed: true only after that ruling."
        )


class KalshiCollector:
    """Collects Kalshi series, events, markets, trades, candlesticks and books."""

    def __init__(self, ctx: CollectContext) -> None:
        require_storage_allowed(ctx.cfg)
        self.ctx = ctx
        self.base = ctx.cfg.venues.kalshi.base_url.rstrip("/")
        self._cutoff: dict[str, int | None] | None = None

    async def cutoff(self) -> dict[str, int | None]:
        """``/historical/cutoff`` as Unix seconds per key (fetched once per run)."""
        if self._cutoff is None:
            res = await self.ctx.http.get(f"{self.base}/historical/cutoff")
            stream = Stream(self.ctx, VENUE, "historical_cutoff")
            stream.put(res)
            stream.commit()
            data = res.data if isinstance(res.data, dict) else {}
            self._cutoff = {
                k: _iso_to_unix(v) for k, v in data.items() if isinstance(v, str) or v is None
            }
        return self._cutoff

    async def series(self, stats: IngestStats) -> None:
        """All series (one response)."""
        stream = Stream(self.ctx, VENUE, "series")
        res = await self.ctx.http.get(f"{self.base}/series", {"include_volume": "true"})
        stats.pages += 1
        stats.items += _count("series")(res.data)
        stream.put(res)
        stream.commit()

    async def events(self, stats: IngestStats, status: str | None = None) -> None:
        """All events, optionally by status (cursor pagination, limit 200)."""
        stream = Stream(self.ctx, VENUE, "events", checkpoint_name=status or "all")
        params: dict[str, str | int] = {"limit": 200}
        if status:
            params["status"] = status
        await cursor_pages(
            self.ctx,
            stream,
            f"{self.base}/events",
            params,
            next_cursor=_cursor,
            items=_count("events"),
            stats=stats,
        )
        stream.commit()

    async def markets(self, stats: IngestStats, status: str | None = "settled") -> None:
        """Live markets closing inside the study window (combos excluded)."""
        stream = Stream(self.ctx, VENUE, "markets", checkpoint_name=status or "all")
        params: dict[str, str | int] = {"limit": 1000, "mve_filter": "exclude"}
        if status:
            params["status"] = status
        if self.ctx.since:
            params["min_close_ts"] = to_unix(self.ctx.since)
        if self.ctx.until:
            params["max_close_ts"] = to_unix(self.ctx.until, end=True)
        await cursor_pages(
            self.ctx,
            stream,
            f"{self.base}/markets",
            params,
            next_cursor=_cursor,
            items=_count("markets"),
            stats=stats,
        )
        stream.commit()

    async def historical_markets(self, stats: IngestStats) -> None:
        """Settled markets older than the cutoff (no time filter exists; filtered later)."""
        stream = Stream(self.ctx, VENUE, "historical_markets")
        await cursor_pages(
            self.ctx,
            stream,
            f"{self.base}/historical/markets",
            {"limit": 1000, "mve_filter": "exclude"},
            next_cursor=_cursor,
            items=_count("markets"),
            stats=stats,
        )
        stream.commit()

    async def trades(self, tickers: Sequence[tuple[str, int, int]], stats: IngestStats) -> None:
        """Trades per market, split at ``trades_created_ts`` into historical and live calls.

        ``tickers`` holds ``(ticker, open_unix, close_unix)``.
        """
        cut = (await self.cutoff()).get("trades_created_ts")
        stream = Stream(self.ctx, VENUE, "trades")
        done = done_set(stream)
        for ticker, open_ts, close_ts in tickers:
            if ticker in done:
                stats.units_skipped += 1
                continue
            plans: list[tuple[str, str, dict[str, str | int]]] = []
            if cut is None or open_ts < cut:
                p: dict[str, str | int] = {"ticker": ticker, "limit": 1000}
                if cut is not None:
                    p["max_ts"] = cut
                plans.append(("historical", f"{self.base}/historical/trades", p))
            if cut is None or close_ts >= cut:
                p = {"ticker": ticker, "limit": 1000}
                if cut is not None:
                    p["min_ts"] = cut
                plans.append(("live", f"{self.base}/markets/trades", p))
            for source, url, params in plans:
                await cursor_pages(
                    self.ctx,
                    stream,
                    url,
                    params,
                    next_cursor=_cursor,
                    items=_count("trades"),
                    state_key=f"cur:{ticker}:{source}",
                    key={"ticker": ticker, "source": source},
                    stats=stats,
                )
            for source, _, _ in plans:
                stream.state.pop(f"cur:{ticker}:{source}", None)
            mark_done(stream, ticker)
            stats.units_done += 1
        stream.commit()

    async def candles(
        self, markets: Sequence[tuple[str, str, int, int, bool]], stats: IngestStats
    ) -> None:
        """Hourly candlesticks per market over its life.

        ``markets`` holds ``(series_ticker, ticker, open_unix, close_unix, settled)``. Markets
        settled before ``market_settled_ts`` use ``/historical/markets/{t}/candlesticks``.
        """
        cut = (await self.cutoff()).get("market_settled_ts")
        stream = Stream(self.ctx, VENUE, "candles")
        done = done_set(stream)
        step = MAX_CANDLES_PER_CALL * CANDLE_PERIOD_MIN * 60
        for series, ticker, open_ts, close_ts, settled in markets:
            if ticker in done:
                stats.units_skipped += 1
                continue
            historical = settled and cut is not None and close_ts < cut
            url = (
                f"{self.base}/historical/markets/{ticker}/candlesticks"
                if historical
                else f"{self.base}/series/{series}/markets/{ticker}/candlesticks"
            )
            start = open_ts
            while start < close_ts:
                end = min(close_ts, start + step)
                res = await self.ctx.http.get(
                    url,
                    {"start_ts": start, "end_ts": end, "period_interval": CANDLE_PERIOD_MIN},
                )
                stats.pages += 1
                stats.items += _count("candlesticks")(res.data)
                stream.put(
                    res,
                    {"ticker": ticker, "series": series, "historical": historical},
                )
                start = end
            mark_done(stream, ticker)
            stats.units_done += 1
        stream.commit()

    async def books(self, tickers: Sequence[str], stats: IngestStats) -> None:
        """One order-book snapshot per ticker (yes bids and no bids)."""
        stream = Stream(self.ctx, VENUE, "books")
        for ticker in tickers:
            res = await self.ctx.http.get(f"{self.base}/markets/{ticker}/orderbook")
            stats.pages += 1
            stream.put(res, {"ticker": ticker, "snapshot_ts": datetime.now(UTC).isoformat()})
        stream.commit()
