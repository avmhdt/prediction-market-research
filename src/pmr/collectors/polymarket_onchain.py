"""Polymarket on-chain fills and the Data API reconciliation (FR-05, DQ-13, D-04).

The default source in the spec, Dune's free tier, has no API access any more
(docs/venue_notes.md), and Polymarket's docs no longer list a public subgraph endpoint. The
collector therefore runs only when ``POLYMARKET_SUBGRAPH_URL`` is set; it queries a graph-node
subgraph with GraphQL over ``GET`` (``?query=``), so the request guard's GET-only rule holds.

:func:`reconcile` is the DQ-13 check: for a sample of markets, at least 99% of Data API trades
must match an on-chain fill by transaction hash, and summed size must differ by at most 0.5%.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from pmr.collectors.base import CollectContext, CollectorError, IngestStats, Stream

VENUE = "polymarket"
MATCH_THRESHOLD = 0.99
SIZE_TOLERANCE = 0.005

FILLS_QUERY = (
    "{orderFilledEvents(first: 1000, orderBy: timestamp, orderDirection: asc, "
    'where: {timestamp_gt: "%d", makerAssetId_in: %s}) '
    "{id transactionHash timestamp maker taker makerAssetId takerAssetId "
    "makerAmountFilled takerAmountFilled fee}}"
)


class OnchainCollector:
    """Order-filled events per token from a configured subgraph (GET only)."""

    def __init__(self, ctx: CollectContext) -> None:
        url = ctx.settings.polymarket_subgraph_url
        if not url:
            raise CollectorError(
                "POLYMARKET_SUBGRAPH_URL is not set; Dune's free tier has no API access, so "
                "on-chain fills need a subgraph endpoint (D-04, docs/venue_notes.md)"
            )
        self.ctx = ctx
        self.url = url
        ctx.http.allow_host(urlsplit(url).hostname or "")

    async def fills(self, token_ids: Sequence[str], stats: IngestStats) -> None:
        """Every OrderFilled event whose maker asset is one of ``token_ids``."""
        stream = Stream(self.ctx, VENUE, "onchain_fills")
        for token in token_ids:
            last_ts = int(stream.state.get(f"ts:{token}", 0))
            while True:
                q = FILLS_QUERY % (last_ts, json.dumps([token]))
                res = await self.ctx.http.get(self.url, {"query": q})
                data = res.data if isinstance(res.data, dict) else {}
                rows = (data.get("data") or {}).get("orderFilledEvents") or []
                stats.pages += 1
                stats.items += len(rows)
                if rows:
                    last_ts = int(rows[-1]["timestamp"])
                    stream.state[f"ts:{token}"] = last_ts
                stream.put(res, {"token_id": token})
                if len(rows) < 1000:
                    break
            stats.units_done += 1
        stream.commit()


@dataclass(frozen=True)
class Reconciliation:
    """DQ-13 result for one market."""

    market_id: str
    n_api: int
    n_matched: int
    size_api: float
    size_onchain: float

    @property
    def match_rate(self) -> float:
        """Share of Data API trades whose transaction hash appears on chain."""
        return self.n_matched / self.n_api if self.n_api else 1.0

    @property
    def size_diff(self) -> float:
        """Relative difference of summed size, on-chain versus Data API."""
        if self.size_api == 0:
            return 0.0 if self.size_onchain == 0 else 1.0
        return abs(self.size_onchain - self.size_api) / self.size_api

    @property
    def passed(self) -> bool:
        """True when both DQ-13 thresholds hold."""
        return self.match_rate >= MATCH_THRESHOLD and self.size_diff <= SIZE_TOLERANCE


def reconcile(
    market_id: str,
    api_trades: Iterable[Mapping[str, object]],
    onchain_fills: Iterable[Mapping[str, object]],
) -> Reconciliation:
    """Compare Data API trades with on-chain fills by transaction hash and summed size.

    Both inputs carry ``tx_hash`` (str) and ``qty`` (shares).
    """
    api = list(api_trades)
    chain = list(onchain_fills)
    chain_hashes = {str(f["tx_hash"]).lower() for f in chain}
    matched = sum(1 for t in api if str(t["tx_hash"]).lower() in chain_hashes)
    return Reconciliation(
        market_id=market_id,
        n_api=len(api),
        n_matched=matched,
        size_api=float(sum(float(str(t["qty"])) for t in api)),
        size_onchain=float(sum(float(str(f["qty"])) for f in chain)),
    )
