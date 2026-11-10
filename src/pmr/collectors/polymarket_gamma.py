"""Polymarket Gamma collector (FR-03): every closed market with tags and rules text.

``GET /markets/keyset`` with ``closed=true`` and ``include_tag=true`` (keyset cursor, limit
100). ``end_date_min`` is set 180 days before the study window, so markets whose scheduled end
date precedes their actual close are kept; the normalizer filters on the close time (DQ-09).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pmr.collectors.base import (
    CollectContext,
    IngestStats,
    Stream,
    cursor_pages,
)

VENUE = "polymarket"
END_DATE_SLACK_DAYS = 180


def _gamma_cursor(data: Any) -> str | None:
    if isinstance(data, dict):
        c = data.get("next_cursor")
        return str(c) if c else None
    return None


def _gamma_count(data: Any) -> int:
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return len(data["markets"])
    return 0


class GammaCollector:
    """Polymarket Gamma metadata."""

    def __init__(self, ctx: CollectContext) -> None:
        self.ctx = ctx
        self.gamma = ctx.cfg.venues.polymarket.gamma_url.rstrip("/")

    async def markets(self, stats: IngestStats) -> None:
        """Every closed market whose scheduled end is at or after the window start minus slack."""
        stream = Stream(self.ctx, VENUE, "gamma_markets")
        params: dict[str, str | int] = {"closed": "true", "include_tag": "true", "limit": 100}
        if self.ctx.since:
            start = self.ctx.since - timedelta(days=END_DATE_SLACK_DAYS)
            params["end_date_min"] = f"{start.isoformat()}T00:00:00Z"
        await cursor_pages(
            self.ctx,
            stream,
            f"{self.gamma}/markets/keyset",
            params,
            next_cursor=_gamma_cursor,
            items=_gamma_count,
            cursor_param="after_cursor",
            stats=stats,
        )
        stream.commit()
