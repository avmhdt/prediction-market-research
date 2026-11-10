"""Polymarket history from public Polygon logs (owner decisions of 2026-11-10).

Never touches Polymarket's servers. Read-only JSON-RPC (``eth_getLogs``, ``eth_blockNumber``,
``eth_getBlockByNumber``) to a public gateway, about 1 request per second, with back-off on
every error (:meth:`pmr.http.client.HttpClient.rpc`, guarded by
:func:`pmr.http.guard.check_jsonrpc`).

* **Market layer** (``--what markets``), complete: CTF condition preparations and resolutions
  for Polymarket's oracles, UMA adapter question events (title and rules in
  ``ancillaryData``), NegRisk adapter and operator events, exchange token registrations, and the
  Protocol V2 rules and lifecycle events. Ranges of up to ``max_block_range`` blocks, halved
  (or narrowed to the gateway's suggested range) when a query hits the 20,000-result cap.
* **Fills** (``--what fills``): ``OrderFilled`` taker rows only (``topics[3]`` = the exchange,
  one row per match) on every exchange: all of them until ``full_fills_until``, then
  ``window_blocks`` out of every ``every_blocks`` (about 10 minutes of every hour).

Every JSON-RPC response is archived unchanged (DQ-12). Collection stops before the raw budget
is used up (``raw_budget_gb``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from pmr.collectors.base import CollectContext, CollectorError, IngestStats, Stream
from pmr.collectors.raw import raw_root

VENUE = "polymarket"
DEFAULT_PATH = Path("config/onchain.yaml")
HINT = re.compile(r"\[(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\]")


def pad_address(addr: str) -> str:
    """An address as a 32-byte topic."""
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


@dataclass(frozen=True)
class OnchainConfig:
    """``config/onchain.yaml``."""

    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> OnchainConfig:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def dir_bytes(path: Path) -> int:
    """Total size of the files under ``path``."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


class OnchainCollector:
    """Read-only Polygon log collector for Polymarket's contracts."""

    def __init__(self, ctx: CollectContext, oc: OnchainConfig | None = None) -> None:
        self.ctx = ctx
        self.oc = oc or OnchainConfig.load()
        self.url = str(self.oc["rpc_url"])
        self.budget = float(self.oc["raw_budget_gb"]) * 1e9
        self._requests = 0
        self._raw_bytes = dir_bytes(raw_root(ctx.data_dir) / VENUE)

    # ----------------------------------------------------------------- basics

    async def _call(self, method: str, params: list[Any]) -> Any:
        res = await self.ctx.http.rpc(self.url, method, params)
        return res

    async def head(self) -> int:
        """Latest block number."""
        res = await self._call("eth_blockNumber", [])
        return int(res.data["result"], 16)

    async def block_time(self, number: int) -> int:
        """Unix time of a block."""
        res = await self._call("eth_getBlockByNumber", [hex(number), False])
        return int(res.data["result"]["timestamp"], 16)

    async def block_at(self, unix: int, lo: int, hi: int) -> int:
        """First block at or after ``unix`` (binary search, about 25 calls)."""
        while lo < hi:
            mid = (lo + hi) // 2
            if await self.block_time(mid) < unix:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _check_budget(self) -> None:
        self._requests += 1
        if self._requests % 200 == 0:
            self._raw_bytes = dir_bytes(raw_root(self.ctx.data_dir) / VENUE)
        if self._raw_bytes >= self.budget:
            raise CollectorError(
                f"raw Polymarket data reached {self._raw_bytes / 1e9:.1f} GB, the budget is "
                f"{self.budget / 1e9:.1f} GB (NFR-13); stopping"
            )

    async def _get_logs(
        self,
        stream: Stream,
        flt: dict[str, Any],
        lo: int,
        hi: int,
        key: dict[str, Any],
        stats: IngestStats,
    ) -> None:
        """Fetch logs for blocks ``[lo, hi]``, splitting the range on the result cap."""
        todo = [(lo, hi)]
        while todo:
            a, b = todo.pop()
            self._check_budget()
            res = await self._call("eth_getLogs", [{**flt, "fromBlock": hex(a), "toBlock": hex(b)}])
            err = res.data.get("error") if isinstance(res.data, dict) else None
            if err:
                msg = str(err.get("message", ""))
                if b <= a:
                    raise CollectorError(f"eth_getLogs failed on a single block {a}: {msg}")
                m = HINT.search(msg)
                mid = int(m.group(2), 16) if m else (a + b) // 2
                mid = min(max(mid, a), b - 1)
                todo += [(mid + 1, b), (a, mid)]  # lower half first
                continue
            rows = res.data.get("result") or []
            stats.pages += 1
            stats.items += len(rows)
            stream.put(res, {**key, "from_block": a, "to_block": b})

    async def _scan(
        self,
        name: str,
        flt: dict[str, Any],
        start: int,
        end: int,
        step: int,
        stats: IngestStats,
        *,
        window: int | None = None,
    ) -> None:
        """Walk ``[start, end]`` in ``step``-block chunks (or sampled windows), resumable."""
        stream = Stream(self.ctx, VENUE, f"onchain_{name}")
        nxt = int(stream.state.get("next_block", start))
        while nxt <= end:
            if window is None:
                lo, hi = nxt, min(end, nxt + step - 1)
            else:
                lo, hi = nxt, min(end, nxt + window - 1)
            await self._get_logs(stream, flt, lo, hi, {"stream": name}, stats)
            nxt = lo + step
            stream.state["next_block"] = nxt
        stream.commit()

    # ----------------------------------------------------------------- tasks

    async def markets(self, stats: IngestStats) -> None:
        """Every market-layer event from ``market_start_block`` to the head."""
        end = await self.head()
        oracles = [pad_address(a) for a in self.oc["oracles"]]
        for ev in self.oc["market_events"]:
            topics: list[Any] = [list(ev["topics"]) if len(ev["topics"]) > 1 else ev["topics"][0]]
            if ev.get("filter_oracles_at"):
                while len(topics) < ev["filter_oracles_at"]:
                    topics.append(None)
                topics.append(oracles)
            flt = {"address": list(ev["addresses"]), "topics": topics}
            start = int(ev.get("from_block", self.oc["market_start_block"]))
            await self._scan(ev["name"], flt, start, end, int(self.oc["max_block_range"]), stats)
            stats.units_done += 1

    async def fills(self, stats: IngestStats) -> None:
        """Taker fills: everything before ``full_fills_until``, then sampled windows."""
        end = await self.head()
        stream = Stream(self.ctx, VENUE, "onchain_meta")
        cutoff = stream.state.get("full_fills_until_block")
        if cutoff is None:
            when = datetime.fromisoformat(str(self.oc["full_fills_until"]).replace("Z", "+00:00"))
            cutoff = await self.block_at(
                int(when.timestamp()), int(self.oc["fills_start_block"]), end
            )
            stream.state["full_fills_until_block"] = cutoff
            stream.commit()
        cutoff = int(cutoff)
        sample = self.oc["sample"]
        step = int(self.oc["max_block_range"])
        for ex in self.oc["exchanges"]:
            flt = {
                "address": ex["address"],
                "topics": [ex["topic"], None, None, pad_address(ex["address"])],
            }
            start = max(int(ex["from_block"]), int(self.oc["fills_start_block"]))
            if start < cutoff:
                await self._scan(f"fills_full_{ex['name']}", flt, start, cutoff - 1, step, stats)
            s_start = max(start, cutoff)
            await self._scan(
                f"fills_sample_{ex['name']}",
                flt,
                s_start,
                end,
                int(sample["every_blocks"]),
                stats,
                window=int(sample["window_blocks"]),
            )
            stats.units_done += 1
