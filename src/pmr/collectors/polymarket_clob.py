"""Polymarket CLOB collector (FR-04): price history and order books.

``GET /prices-history``: the whole life at 12-hour fidelity (``interval=all``), plus hourly
bars over the last 32 days before close, requested in windows of at most 15 days (the
endpoint's limit, measured 2026-11-10). ``GET /book``: one snapshot per token.
"""

from __future__ import annotations

from collections.abc import Sequence

from pmr.collectors.base import (
    CollectContext,
    IngestStats,
    Stream,
    done_set,
    mark_done,
)

VENUE = "polymarket"
HOURLY_LOOKBACK_S = 32 * 86400
CLOB_MAX_WINDOW_S = 15 * 86400


class ClobCollector:
    """Polymarket CLOB public reads."""

    def __init__(self, ctx: CollectContext) -> None:
        self.ctx = ctx
        self.clob = ctx.cfg.venues.polymarket.clob_url.rstrip("/")

    async def prices(self, tokens: Sequence[tuple[str, str, int]], stats: IngestStats) -> None:
        """Price history per YES token: 12-hour life series plus hourly bars before close.

        ``tokens`` holds ``(condition_id, yes_token_id, close_unix)``.
        """
        stream = Stream(self.ctx, VENUE, "clob_prices")
        done = done_set(stream)
        for cid, token, close_ts in tokens:
            if token in done:
                stats.units_skipped += 1
                continue
            res = await self.ctx.http.get(
                f"{self.clob}/prices-history",
                {"market": token, "interval": "all", "fidelity": 720},
            )
            stats.pages += 1
            stream.put(res, {"condition_id": cid, "token_id": token, "fidelity_min": 720})
            start = close_ts - HOURLY_LOOKBACK_S
            while start < close_ts:
                end = min(close_ts, start + CLOB_MAX_WINDOW_S)
                res = await self.ctx.http.get(
                    f"{self.clob}/prices-history",
                    {"market": token, "startTs": start, "endTs": end, "fidelity": 60},
                )
                stats.pages += 1
                stream.put(res, {"condition_id": cid, "token_id": token, "fidelity_min": 60})
                start = end
            mark_done(stream, token)
            stats.units_done += 1
        stream.commit()

    async def books(self, tokens: Sequence[str], stats: IngestStats) -> None:
        """One order-book snapshot per token."""
        stream = Stream(self.ctx, VENUE, "clob_books")
        for token in tokens:
            res = await self.ctx.http.get(f"{self.clob}/book", {"token_id": token})
            stats.pages += 1
            stream.put(res, {"token_id": token})
        stream.commit()
