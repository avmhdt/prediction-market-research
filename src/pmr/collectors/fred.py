"""FRED DTB3 collector (D-10): 3-month T-bill rate, daily, public CSV without a key.

Source: Board of Governors of the Federal Reserve System (US), 3-Month Treasury Bill Secondary
Market Rate, Discount Basis [DTB3], retrieved from FRED, Federal Reserve Bank of St. Louis;
https://fred.stlouisfed.org/series/DTB3.
"""

from __future__ import annotations

from pmr.collectors.base import CollectContext, IngestStats, Stream

VENUE = "fred"
DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"


class FredCollector:
    """Downloads the whole DTB3 series (under 1 MB)."""

    def __init__(self, ctx: CollectContext) -> None:
        self.ctx = ctx

    async def dtb3(self, stats: IngestStats) -> None:
        """Fetch the CSV and archive it as one raw record."""
        stream = Stream(self.ctx, VENUE, "dtb3")
        res = await self.ctx.http.get(DTB3_URL, expect_json=False)
        stats.pages += 1
        text = res.data.get("text", "") if isinstance(res.data, dict) else ""
        stats.items += max(0, str(text).count("\n") - 1)
        stream.put(res, {"series": "DTB3"})
        stream.commit()
