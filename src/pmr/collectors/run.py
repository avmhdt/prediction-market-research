"""Entry points behind ``pmr ingest ...`` (SPEC section 7e).

Per-market endpoints (trades, prices, candles, bets) take their market set from the
normalized markets in ``<data>/interim/markets``, so the order is: ingest metadata,
``pmr normalize``, then ingest the per-market endpoints.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path

import polars as pl

from pmr.collectors.base import CollectContext, CollectorError, IngestStats
from pmr.collectors.fred import FredCollector
from pmr.collectors.kalshi import KalshiCollector, require_storage_allowed
from pmr.collectors.manifold import ManifoldCollector
from pmr.collectors.metaculus import MetaculusCollector
from pmr.collectors.polymarket_clob import ClobCollector
from pmr.collectors.polymarket_data import DataApiCollector
from pmr.collectors.polymarket_gamma import GammaCollector
from pmr.collectors.polymarket_onchain import OnchainCollector
from pmr.http.client import HttpClient, rate_limits_from_config
from pmr.normalize.store import current_markets
from pmr.settings import PmrConfig, Settings

MIN_NOTIONAL_FOR_TRADES = 100.0  # DQ-07: a market below USD 100 lifetime volume cannot pass


def _ns(d: date | None, *, end: bool = False) -> int | None:
    from pmr.collectors.base import to_unix

    return None if d is None else to_unix(d, end=end) * 1_000_000_000


def window_markets(
    data_dir: Path, venue: str, since: date | None, until: date | None
) -> pl.DataFrame:
    """Current normalized markets of ``venue`` whose close time falls in the window."""
    m = current_markets(data_dir, venue)
    if m.is_empty():
        return m
    lo, hi = _ns(since), _ns(until, end=True)
    if lo is not None:
        m = m.filter(pl.col("close_ts") >= lo)
    if hi is not None:
        m = m.filter(pl.col("close_ts") < hi)
    return m.sort("market_id")


async def _with_ctx(
    settings: Settings,
    cfg: PmrConfig,
    data_dir: Path,
    since: date | None,
    until: date | None,
    body: Callable[[CollectContext, IngestStats], Awaitable[None]],
) -> IngestStats:
    stats = IngestStats()
    async with HttpClient(
        settings, cfg.http, limits=rate_limits_from_config(cfg), seed=cfg.seed
    ) as http:
        ctx = CollectContext(
            http=http, settings=settings, cfg=cfg, data_dir=data_dir, since=since, until=until
        )
        await body(ctx, stats)
    return stats


async def ingest_kalshi(
    settings: Settings,
    cfg: PmrConfig,
    data_dir: Path,
    what: str,
    *,
    since: date | None,
    until: date | None,
    status: str | None,
) -> IngestStats:
    """``pmr ingest kalshi --what ...``."""
    # Refuse before any request while storing Kalshi data is not allowed.
    require_storage_allowed(cfg)

    async def body(ctx: CollectContext, stats: IngestStats) -> None:
        k = KalshiCollector(ctx)
        if what == "series":
            await k.series(stats)
        elif what == "events":
            await k.events(stats, status)
        elif what == "markets":
            await k.markets(stats, status or "settled")
        elif what == "historical":
            await k.historical_markets(stats)
        elif what in ("trades", "candles", "books"):
            m = window_markets(data_dir, "kalshi", since, until)
            if what == "trades":
                # Kalshi volume is in contracts; below 100 contracts the notional cannot reach
                # USD 100, so the market can never pass DQ-07 (decisions A-28).
                m = m.filter(pl.col("volume").fill_null(0) >= MIN_NOTIONAL_FOR_TRADES)
                rows = [
                    (r["symbol"], (r["open_ts"] or 0) // 10**9, (r["close_ts"] or 0) // 10**9)
                    for r in m.iter_rows(named=True)
                ]
                await k.trades(rows, stats)
            elif what == "candles":
                m = m.filter(pl.col("volume").fill_null(0) > 0)
                rows5 = [
                    (
                        r["series_id"] or "",
                        r["symbol"],
                        (r["open_ts"] or 0) // 10**9,
                        (r["close_ts"] or 0) // 10**9,
                        r["status"] in ("resolved", "voided"),
                    )
                    for r in m.iter_rows(named=True)
                ]
                await k.candles(rows5, stats)
            else:
                await k.books(m["symbol"].to_list(), stats)
        else:
            raise CollectorError(f"unknown --what {what!r}")

    return await _with_ctx(settings, cfg, data_dir, since, until, body)


async def ingest_polymarket(
    settings: Settings,
    cfg: PmrConfig,
    data_dir: Path,
    source: str,
    *,
    since: date | None,
    until: date | None,
) -> IngestStats:
    """``pmr ingest polymarket --source gamma|data|clob|onchain``."""

    async def body(ctx: CollectContext, stats: IngestStats) -> None:
        if source == "gamma":
            await GammaCollector(ctx).markets(stats)
            return
        if source.startswith("onchain"):
            if source == "onchain-markets":
                await OnchainCollector(ctx).markets(stats)
            elif source == "onchain-fills":
                await OnchainCollector(ctx).fills(stats)
            else:
                raise CollectorError(f"unknown --source {source!r}")
            return
        m = window_markets(data_dir, "polymarket", since, until)
        if source == "data":
            m = m.filter(pl.col("volume").fill_null(0) >= MIN_NOTIONAL_FOR_TRADES)
            await DataApiCollector(ctx).trades(m["market_id"].to_list(), stats)
        elif source == "clob":
            rows = [
                (r["market_id"], r["symbol"], (r["close_ts"] or 0) // 10**9)
                for r in m.iter_rows(named=True)
            ]
            await ClobCollector(ctx).prices(rows, stats)
        else:
            raise CollectorError(f"unknown --source {source!r}")

    return await _with_ctx(settings, cfg, data_dir, since, until, body)


async def ingest_manifold(
    settings: Settings,
    cfg: PmrConfig,
    data_dir: Path,
    what: str,
    *,
    since: date | None,
    until: date | None,
) -> IngestStats:
    """``pmr ingest manifold [--what markets|details|bets]``."""

    async def body(ctx: CollectContext, stats: IngestStats) -> None:
        mc = ManifoldCollector(ctx)
        if what == "markets":
            await mc.markets(stats)
            return
        m = window_markets(data_dir, "manifold", since, until).filter(
            pl.col("n_traders").fill_null(0) >= cfg.venues.manifold.min_unique_traders
        )
        if what == "details":
            m = m.filter(pl.col("status").is_in(["resolved", "voided"]))
            await mc.details(m["market_id"].to_list(), stats)
        elif what == "bets":
            m = m.filter(pl.col("status").is_in(["resolved", "voided"]))
            await mc.bets(m["market_id"].to_list(), stats)
        else:
            raise CollectorError(f"unknown --what {what!r}")

    return await _with_ctx(settings, cfg, data_dir, since, until, body)


async def ingest_metaculus(
    settings: Settings, cfg: PmrConfig, data_dir: Path, *, since: date | None, until: date | None
) -> IngestStats:
    """``pmr ingest metaculus`` (needs METACULUS_API_TOKEN)."""
    if settings.metaculus_api_token is None:
        raise CollectorError("METACULUS_API_TOKEN is not set (P-06)")

    async def body(ctx: CollectContext, stats: IngestStats) -> None:
        await MetaculusCollector(ctx).posts(stats)

    return await _with_ctx(settings, cfg, data_dir, since, until, body)


async def ingest_fred(settings: Settings, cfg: PmrConfig, data_dir: Path) -> IngestStats:
    """``pmr ingest fred``."""

    async def body(ctx: CollectContext, stats: IngestStats) -> None:
        await FredCollector(ctx).dtb3(stats)

    return await _with_ctx(settings, cfg, data_dir, None, None, body)
