"""Manifold collector (FR-06): the play-money contrast. Public reads, 500 requests/min per IP.

1. ``GET /v0/markets`` (newest first, ``before`` = last market id): every market's lite record.
2. For each eligible market (binary, resolved, closed in the study window, at least
   ``min_unique_traders`` bettors; D-01): ``GET /v0/market/{id}`` for the full record
   (resolution, rules text, groups).
3. For the same markets: ``GET /v0/bets`` (``before`` = last bet id, limit 1000) for the bets
   with ``probBefore`` / ``probAfter``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pmr.collectors.base import (
    CollectContext,
    IngestStats,
    Stream,
    cursor_pages,
    done_set,
    mark_done,
)

VENUE = "manifold"
PAGE = 1000


def _last_id(data: Any) -> str | None:
    if isinstance(data, list) and len(data) == PAGE:
        last = data[-1]
        if isinstance(last, dict) and last.get("id"):
            return str(last["id"])
    return None


def _len(data: Any) -> int:
    return len(data) if isinstance(data, list) else 0


class ManifoldCollector:
    """Manifold markets, full market records and bets."""

    def __init__(self, ctx: CollectContext) -> None:
        self.ctx = ctx
        self.base = ctx.cfg.venues.manifold.base_url.rstrip("/")

    async def markets(self, stats: IngestStats) -> None:
        """Every market's lite record, newest first."""
        stream = Stream(self.ctx, VENUE, "markets")
        await cursor_pages(
            self.ctx,
            stream,
            f"{self.base}/markets",
            {"limit": PAGE},
            next_cursor=_last_id,
            items=_len,
            cursor_param="before",
            stats=stats,
        )
        stream.commit()

    async def details(self, market_ids: Sequence[str], stats: IngestStats) -> None:
        """Full market record (resolution, rules text, groups) for each market id."""
        stream = Stream(self.ctx, VENUE, "market_details")
        done = done_set(stream)
        for mid in market_ids:
            if mid in done:
                stats.units_skipped += 1
                continue
            res = await self.ctx.http.get(f"{self.base}/market/{mid}")
            stats.pages += 1
            mark_done(stream, mid)
            stream.put(res, {"market_id": mid})
            stats.units_done += 1
        stream.commit()

    async def bets(self, market_ids: Sequence[str], stats: IngestStats) -> None:
        """All bets for each market id (newest first, ``before`` cursor)."""
        stream = Stream(self.ctx, VENUE, "bets")
        done = done_set(stream)
        for mid in market_ids:
            if mid in done:
                stats.units_skipped += 1
                continue
            await cursor_pages(
                self.ctx,
                stream,
                f"{self.base}/bets",
                {"contractId": mid, "limit": PAGE},
                next_cursor=_last_id,
                items=_len,
                cursor_param="before",
                state_key=f"cur:{mid}",
                key={"market_id": mid},
                stats=stats,
            )
            stream.state.pop(f"cur:{mid}", None)
            mark_done(stream, mid)
            stats.units_done += 1
        stream.commit()
