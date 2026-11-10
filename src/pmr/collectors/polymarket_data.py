"""Polymarket Data API v2 collector (FR-04): every fill per market.

``GET /v2/trades?condition=<id>`` with cursor pagination; the default ``taker_only=true``
gives one row per taker fill. v1 was retired on 2026-11-02. Queries by condition serve a
fixed three-year window (docs/venue_notes.md).
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

VENUE = "polymarket"


def _v2_cursor(data: Any) -> str | None:
    if isinstance(data, dict):
        pag = data.get("pagination") or {}
        if isinstance(pag, dict) and pag.get("has_more") and pag.get("next_cursor"):
            return str(pag["next_cursor"])
    return None


def _v2_count(data: Any) -> int:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return len(data["data"])
    return 0


class DataApiCollector:
    """Polymarket Data API v2 trades."""

    def __init__(self, ctx: CollectContext) -> None:
        self.ctx = ctx
        self.data = ctx.cfg.venues.polymarket.data_url.rstrip("/")

    async def trades(self, condition_ids: Sequence[str], stats: IngestStats) -> None:
        """All taker fills per market from Data API v2 (cursor pagination)."""
        stream = Stream(self.ctx, VENUE, "data_trades")
        done = done_set(stream)
        for cid in condition_ids:
            if cid in done:
                stats.units_skipped += 1
                continue
            await cursor_pages(
                self.ctx,
                stream,
                f"{self.data}/trades",
                {"condition": cid, "limit": 1000},
                next_cursor=_v2_cursor,
                items=_v2_count,
                state_key=f"cur:{cid}",
                key={"condition_id": cid},
                stats=stats,
            )
            stream.state.pop(f"cur:{cid}", None)
            mark_done(stream, cid)
            stats.units_done += 1
        stream.commit()
