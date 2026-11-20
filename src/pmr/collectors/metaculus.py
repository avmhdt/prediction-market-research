"""Metaculus collector (FR-07): binary questions, resolutions and community-prediction history.

Every Metaculus endpoint requires ``Authorization: Token <token>`` (P-06); without
``METACULUS_API_TOKEN`` the collector refuses to start and makes no request. Default accounts
receive no community-prediction history; the "Unrestricted" tier is needed for it
(docs/venue_notes.md).

``GET /api/posts/`` with ``forecast_type=binary``, ``statuses=resolved``,
``include_cp_history=true``, offset pagination (``next`` link).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from pmr.collectors.base import CollectContext, CollectorError, IngestStats, Stream, to_unix

VENUE = "metaculus"
PAGE = 100


def next_offset(data: Any) -> str | None:
    """The ``offset`` of the response's ``next`` link, if any."""
    if isinstance(data, dict) and data.get("next"):
        q = parse_qs(urlsplit(str(data["next"])).query)
        off = q.get("offset")
        return off[0] if off else None
    return None


def _count(data: Any) -> int:
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return len(data["results"])
    return 0


class MetaculusCollector:
    """Metaculus binary questions (token required)."""

    def __init__(self, ctx: CollectContext) -> None:
        if ctx.settings.metaculus_api_token is None:
            raise CollectorError(
                "METACULUS_API_TOKEN is not set (P-06); the Metaculus API rejects every "
                "unauthenticated request"
            )
        self.ctx = ctx
        self.base = ctx.cfg.venues.metaculus.base_url.rstrip("/")
        token = ctx.settings.metaculus_api_token.get_secret_value()
        self.headers = {"Authorization": f"Token {token}"}

    async def posts(self, stats: IngestStats) -> None:
        """Resolved binary questions with community-prediction history where the tier allows."""
        stream = Stream(self.ctx, VENUE, "posts")
        params: dict[str, str | int] = {
            "forecast_type": "binary",
            "statuses": "resolved",
            "include_cp_history": "true",
            "include_descriptions": "true",
            "limit": PAGE,
            "order_by": "-scheduled_resolve_time",
        }
        if self.ctx.since:
            params["scheduled_resolve_time__gte"] = str(to_unix(self.ctx.since))
        st = stream.state.setdefault("cursor", {})
        if st.get("done"):
            return
        offset = int(st.get("offset", 0))
        while True:
            res = await self.ctx.http.get(
                f"{self.base}/posts/", {**params, "offset": offset}, headers=self.headers
            )
            n = _count(res.data)
            nxt = next_offset(res.data)
            stats.pages += 1
            stats.items += n
            stream.state["cursor"] = {"offset": int(nxt)} if nxt and n else {"done": True}
            stream.put(res)
            if not nxt or not n:
                break
            offset = int(nxt)
        stream.commit()
