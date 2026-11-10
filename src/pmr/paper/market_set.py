"""The paper run's market set (FR-23): confirmed pairs, explicit extras, then the most active
open markets per venue, refreshed every ``refresh_hours``.

* Kalshi: ``GET /markets?status=open&mve_filter=exclude`` (all pages), ranked by 24-hour volume;
  series fee type and multiplier from ``GET /series``; each event's ``mutually_exclusive`` flag
  from ``GET /events/{event_ticker}`` (multi-outcome arb sets).
* Polymarket (owner decision 2026-11-10: public reads only to choose this set and poll its
  books): Gamma ``GET /markets/keyset?closed=false&order=volume24hr`` with tags. Each market's
  own ``feeSchedule.rate`` (0 when ``feesEnabled`` is false) and tick size are used.

Markets that close within ``min_days_to_close`` are skipped. A market stays in the set while
it is open; the set's history is kept in ``<data>/paper/market_set.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pmr.http.client import HttpClient
from pmr.normalize.categories import CategoryMap
from pmr.normalize.util import fnum, iso_ns, json_list

NS = 10**9


@dataclass(frozen=True)
class MarketMeta:
    """What the recorder, strategies and fee model need about one market."""

    venue: str
    symbol: str
    market_id: str
    event_id: str | None
    title: str
    category: str
    close_ts: int
    no_symbol: str | None = None
    tick: float = 0.01
    fee_multiplier: float | None = None
    fee_type: str | None = None
    fee_rate: float | None = None
    mutually_exclusive: bool = False
    event_size: int = 0  # open outcomes in a mutually exclusive event (0 = unknown)
    volume_24h: float = 0.0

    @property
    def key(self) -> str:
        """``venue:symbol``."""
        return f"{self.venue}:{self.symbol}"

    @property
    def maker_fee_unknown(self) -> bool:
        """Kalshi series with maker fees whose coefficient is not transcribed yet (M3)."""
        return self.venue == "kalshi" and bool(self.fee_type and "maker" in self.fee_type)


async def kalshi_candidates(
    http: HttpClient, base: str, cats: CategoryMap, *, now_ns: int, min_days: float, limit: int
) -> list[MarketMeta]:
    """Top open Kalshi markets by 24-hour volume."""
    series_res = await http.get(f"{base}/series")
    series: dict[str, dict[str, Any]] = {}
    body = series_res.data if isinstance(series_res.data, dict) else {}
    for s in body.get("series", []):
        series[str(s.get("ticker"))] = s
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {
            "status": "open",
            "mve_filter": "exclude",
            "limit": 1000,
            "min_close_ts": (now_ns // NS) + int(min_days * 86400),
        }
        if cursor:
            params["cursor"] = cursor
        res = await http.get(f"{base}/markets", params)
        d = res.data if isinstance(res.data, dict) else {}
        markets += [m for m in d.get("markets", []) if isinstance(m, dict)]
        cursor = d.get("cursor") or None
        if not cursor or not d.get("markets"):
            break
    markets.sort(key=lambda m: (-(fnum(m.get("volume_24h_fp")) or 0.0), str(m.get("ticker"))))
    top = markets[:limit]
    out: list[MarketMeta] = []
    events: dict[str, dict[str, Any]] = {}
    for m in top:
        ev_t = str(m.get("event_ticker") or "")
        if ev_t and ev_t not in events:
            r = await http.get(f"{base}/events/{ev_t}", {"with_nested_markets": "true"})
            body_ev = r.data if isinstance(r.data, dict) else {}
            ev = body_ev.get("event", {}) if isinstance(body_ev.get("event"), dict) else {}
            nested = ev.get("markets") or body_ev.get("markets") or []
            ev = {
                **ev,
                "_open": sum(1 for x in nested if str(x.get("status")) in ("active", "open")),
            }
            events[ev_t] = ev
        ev = events.get(ev_t, {})
        s = series.get(str(ev.get("series_ticker") or ""), {})
        out.append(
            MarketMeta(
                venue="kalshi",
                symbol=str(m["ticker"]),
                market_id=str(m["ticker"]),
                event_id=ev_t or None,
                title=str(m.get("title") or ""),
                category=cats.map("kalshi", [s.get("category"), ev.get("category")]),
                close_ts=iso_ns(m.get("close_time")) or 0,
                tick=0.01,
                fee_multiplier=fnum(s.get("fee_multiplier")),
                fee_type=s.get("fee_type"),
                mutually_exclusive=bool(ev.get("mutually_exclusive")),
                event_size=int(ev.get("_open") or 0),
                volume_24h=fnum(m.get("volume_24h_fp")) or 0.0,
            )
        )
    return out


async def polymarket_candidates(
    http: HttpClient, gamma: str, cats: CategoryMap, *, now_ns: int, min_days: float, limit: int
) -> list[MarketMeta]:
    """Top open Polymarket markets by 24-hour volume (one or two Gamma pages)."""
    out: list[MarketMeta] = []
    sizes: dict[str, int] = {}
    cursor: str | None = None
    horizon = now_ns + int(min_days * 86400 * NS)
    for _ in range(10):
        params: dict[str, str | int] = {
            "closed": "false",
            "include_tag": "true",
            "order": "volume24hr",
            "ascending": "false",
            "limit": 100,
        }
        if cursor:
            params["after_cursor"] = cursor
        res = await http.get(f"{gamma}/markets/keyset", params)
        d = res.data if isinstance(res.data, dict) else {}
        for m in d.get("markets", []):
            tokens = [str(t) for t in json_list(m.get("clobTokenIds"))]
            end = iso_ns(m.get("endDate")) or 0
            if len(tokens) != 2 or end < horizon or not m.get("acceptingOrders", True):
                continue
            ev = (m.get("events") or [{}])[0] or {}
            ev_size = 0
            if ev.get("negRisk") and ev.get("id") is not None:
                ev_id = str(ev["id"])
                if ev_id not in sizes:
                    r = await http.get(f"{gamma}/events/{ev_id}")
                    body = r.data if isinstance(r.data, dict) else {}
                    sizes[ev_id] = sum(1 for x in body.get("markets") or [] if not x.get("closed"))
                ev_size = sizes[ev_id]
            tags = [t.get("label") for t in (m.get("tags") or []) if isinstance(t, dict)]
            sched = m.get("feeSchedule") or {}
            rate = 0.0 if not m.get("feesEnabled") else fnum(sched.get("rate"))
            out.append(
                MarketMeta(
                    venue="polymarket",
                    symbol=tokens[0],
                    market_id=str(m.get("conditionId")),
                    event_id=str(ev.get("id")) if ev.get("id") is not None else None,
                    title=str(m.get("question") or ""),
                    category=cats.map("polymarket", [str(t) for t in tags if t]),
                    close_ts=end,
                    no_symbol=tokens[1],
                    tick=fnum(m.get("orderPriceMinTickSize")) or 0.01,
                    fee_rate=rate,
                    mutually_exclusive=bool(ev.get("negRisk")),
                    event_size=ev_size,
                    volume_24h=fnum(m.get("volume24hr")) or 0.0,
                )
            )
            if len(out) >= limit:
                return out
        cursor = d.get("next_cursor")
        if not cursor:
            break
    return out


@dataclass
class MarketSet:
    """Current members plus the add/remove history."""

    members: dict[str, MarketMeta]
    history: list[dict[str, Any]]
    refreshed_ns: int = 0

    @classmethod
    def load(cls, path: Path) -> MarketSet:
        """Load ``market_set.json`` (empty set when absent)."""
        if not path.exists():
            return cls({}, [])
        d = json.loads(path.read_text(encoding="utf-8"))
        members = {k: MarketMeta(**v) for k, v in d.get("members", {}).items()}
        return cls(members, d.get("history", []), int(d.get("refreshed_ns", 0)))

    def save(self, path: Path) -> None:
        """Write atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "refreshed_ns": self.refreshed_ns,
                    "members": {k: asdict(v) for k, v in sorted(self.members.items())},
                    "history": self.history,
                },
                indent=1,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def update(
        self,
        candidates: dict[str, list[MarketMeta]],
        quotas: dict[str, int],
        now_ns: int,
        max_markets: int,
    ) -> None:
        """Drop closed members; top each venue up to its quota from ``candidates``."""
        for key, m in list(self.members.items()):
            if m.close_ts <= now_ns:
                del self.members[key]
                self.history.append(
                    {"ts": now_ns, "event": "removed", "key": key, "reason": "closed"}
                )
        for venue, cands in candidates.items():
            have = sum(1 for m in self.members.values() if m.venue == venue)
            for m in cands:
                if have >= quotas.get(venue, 0) or len(self.members) >= max_markets:
                    break
                if m.key in self.members:
                    continue
                self.members[m.key] = m
                self.history.append({"ts": now_ns, "event": "added", "key": m.key})
                have += 1
        self.refreshed_ns = now_ns
