"""Book recorder and paper-trading loop (FR-23, FR-24, DQ-01, DQ-14, NFR-09).

Every ``poll_interval_s`` seconds, for the market set (:mod:`pmr.paper.market_set`):

* Kalshi: ``GET /markets/orderbooks`` in batches of 100 tickers, and new trades from
  ``GET /markets/trades?min_ts=...`` (the venue-wide feed, filtered to the set);
* Polymarket: ``GET /book`` for each YES token (and NO token). Public, unauthenticated reads
  only (owner decision 2026-11-10). A 401, 403 or 451 answer stops Polymarket polling at once
  and is reported; it is never retried another way.

Responses are appended unchanged to the raw archive (``<venue>/rec_books``,
``kalshi/rec_trades``). The paper strategies run on each cycle and their events go to the
ledger. Side files in ``<data>/paper/``: ``heartbeat.json`` (every cycle), ``gaps.csv``
(no successful poll of a venue for more than 3 polling intervals, including downtime between
runs; never interpolated), ``run.json`` (first start) and ``market_set.json``.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import shutil
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import structlog

from pmr.analysis.arb import ArbConfig
from pmr.collectors.raw import RawWriter, record_from_fetch
from pmr.fees.table import FeeTable
from pmr.http.client import FetchResult, HttpClient, HttpError
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.normalize import kalshi as nk
from pmr.normalize import polymarket as npm
from pmr.normalize.categories import CategoryMap
from pmr.paper.config import PaperConfig
from pmr.paper.market_set import MarketMeta, MarketSet, kalshi_candidates, polymarket_candidates
from pmr.paper.strategies import ArbMonitor, Cycle, FLBTilt, MMQuotes, TradePrint
from pmr.paper.trader import Ledger, restore
from pmr.settings import PmrConfig, Settings
from pmr.sim.mm import MMParams

log = structlog.get_logger("pmr.paper")
NS = 10**9
BLOCKED = {401, 403, 451}
GAP_COLUMNS = ("venue", "start_ts", "end_ts", "seconds", "reason")


def book_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """``side, price, qty`` frame from normalized book rows."""
    return pl.DataFrame(
        {
            "side": [r["side"] for r in rows],
            "price": [r["price"] for r in rows],
            "qty": [r["qty"] for r in rows],
        },
        schema={"side": pl.Int8, "price": pl.Float64, "qty": pl.Float64},
    )


def mid(book: pl.DataFrame) -> float | None:
    """Mid of best bid and ask, when both exist and do not cross."""
    bids = book.filter(pl.col("side") == 1)["price"]
    asks = book.filter(pl.col("side") == -1)["price"]
    if bids.is_empty() or asks.is_empty():
        return None
    bb, ba = float(bids.max()), float(asks.min())  # type: ignore[arg-type]
    return (bb + ba) / 2 if ba > bb else None


@dataclass
class VenueHealth:
    """Per-venue poll bookkeeping."""

    last_success_ns: int | None = None
    failures: int = 0
    stopped: str | None = None
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))


class Recorder:
    """Polls books and trades for the market set and drives the paper strategies."""

    def __init__(
        self,
        settings: Settings,
        cfg: PmrConfig,
        pcfg: PaperConfig,
        data_dir: Path,
        cats: CategoryMap,
        fees: FeeTable,
        *,
        paper: bool = True,
        http: HttpClient | None = None,
        clock: Callable[[], float] = time.time,
        config_hash: str = "",
    ) -> None:
        self.settings, self.cfg, self.pcfg = settings, cfg, pcfg
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "paper"
        self.cats, self.fees = cats, fees
        self.paper = paper
        self.clock = clock
        limits = RateLimiterRegistry(default_rps=2.0, per_host=dict(pcfg.rate_limits_rps))
        self.http = http or HttpClient(settings, cfg.http, limits=limits, seed=cfg.seed)
        self.kalshi = cfg.venues.kalshi.base_url.rstrip("/")
        self.gamma = cfg.venues.polymarket.gamma_url.rstrip("/")
        self.clob = cfg.venues.polymarket.clob_url.rstrip("/")
        self.health = {"kalshi": VenueHealth(), "polymarket": VenueHealth()}
        self.set = MarketSet.load(self.dir / "market_set.json")
        self.writers = {
            ("kalshi", "rec_books"): RawWriter(self.data_dir, "kalshi", "rec_books", 10**9),
            ("kalshi", "rec_trades"): RawWriter(self.data_dir, "kalshi", "rec_trades", 10**9),
            ("polymarket", "rec_books"): RawWriter(self.data_dir, "polymarket", "rec_books", 10**9),
        }
        self.kalshi_trade_ts: int | None = None
        self.seen_trades: deque[str] = deque(maxlen=50_000)
        self.seen_set: set[str] = set()
        self.cycle_n = 0
        self.pending_settle: dict[str, MarketMeta] = {}
        self.last_disk_check = 0.0
        self.disk_warn = False
        self.offsets: list[float] = []
        self.ledger = Ledger(self.dir, config_hash)
        st = pcfg.strategies
        mm = cfg.mm
        self.strategies: list[Any] = []
        flb_done = restore(self.ledger) if paper else set()
        if paper:
            arb_cfg = ArbConfig(
                fees=fees,
                r_annual=latest_dtb3(self.data_dir),
                collateral_yield=dict(cfg.arb.collateral_yield),
                settlement_lag_hours={
                    "kalshi": cfg.venues.kalshi.settlement_lag_hours,
                    "polymarket": cfg.venues.polymarket.settlement_lag_hours,
                },
                transfer_cost_per_contract=cfg.arb.transfer_cost_per_contract,
                divergence_prior=cfg.arb.divergence_prior,
            )
            self.arb_cfg = arb_cfg
            if st.arb_monitor.enabled:
                self.strategies.append(ArbMonitor(st.arb_monitor, arb_cfg))
            if st.mm_quotes.enabled:
                base = MMParams(
                    half_spread=mm.half_spread,
                    skew_gamma=mm.skew_gamma,
                    q_max=st.mm_quotes.q_max,
                    capital_max=st.mm_quotes.capital_max,
                    ema_halflife_s=mm.ema_halflife_s,
                )
                self.strategies.append(MMQuotes(st.mm_quotes, base, fees))
            if st.flb_tilt.enabled:
                self.strategies.append(FLBTilt(st.flb_tilt, fees, done=flb_done))

    # ------------------------------------------------------------------ helpers

    def now_ns(self) -> int:
        return int(self.clock() * NS)

    def _store(self, venue: str, endpoint: str, res: FetchResult, key: dict[str, Any]) -> None:
        self.writers[(venue, endpoint)].write(
            record_from_fetch(res, venue=venue, endpoint=endpoint, key=key)
        )

    def _gap(self, venue: str, start: int, end: int, reason: str) -> None:
        path = self.dir / "gaps.csv"
        new = not path.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if new:
                w.writerow(GAP_COLUMNS)
            w.writerow([venue, start, end, round((end - start) / NS, 3), reason])
        if self.paper:
            self.ledger.write(
                [
                    {
                        "type": "gap",
                        "ts": end,
                        "venue": venue,
                        "start_ts": start,
                        "end_ts": end,
                        "reason": reason,
                    }
                ]
            )

    def _start_bookkeeping(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        run = self.dir / "run.json"
        now = self.now_ns()
        if not run.exists():
            run.write_text(json.dumps({"first_start_ns": now}), encoding="utf-8")
        hb = self.dir / "heartbeat.json"
        if hb.exists():
            prev = json.loads(hb.read_text(encoding="utf-8"))
            limit = 3 * self.pcfg.poll_interval_s * NS
            for venue, last in (prev.get("last_success_ns") or {}).items():
                if venue in self.health and last:
                    self.health[venue].last_success_ns = int(last)
                    if now - int(last) > limit:
                        self._gap(venue, int(last), now, "downtime")
                        # The outage is logged; the first cycle must not log it again.
                        self.health[venue].last_success_ns = now

    # ------------------------------------------------------------------ market set

    async def refresh(self) -> list[dict[str, Any]]:
        """Re-select the set when due; settle markets that left it when results are known."""
        now = self.now_ns()
        events: list[dict[str, Any]] = []
        due = now - self.set.refreshed_ns >= self.pcfg.markets.refresh_hours * 3600 * NS
        if due:
            before = set(self.set.members)
            m = self.pcfg.markets
            cands: dict[str, list[MarketMeta]] = {}
            try:
                cands["kalshi"] = await kalshi_candidates(
                    self.http,
                    self.kalshi,
                    self.cats,
                    now_ns=now,
                    min_days=m.min_days_to_close,
                    limit=3 * m.per_venue.get("kalshi", 0),
                )
            except HttpError as exc:
                log.warning("refresh_failed", venue="kalshi", error=str(exc))
            if self.health["polymarket"].stopped is None and m.per_venue.get("polymarket"):
                try:
                    cands["polymarket"] = await polymarket_candidates(
                        self.http,
                        self.gamma,
                        self.cats,
                        now_ns=now,
                        min_days=m.min_days_to_close,
                        limit=3 * m.per_venue["polymarket"],
                    )
                except HttpError as exc:
                    self._maybe_stop("polymarket", exc)
            self.set.update(cands, dict(m.per_venue), now, m.max_markets)
            self.set.save(self.dir / "market_set.json")
            after = set(self.set.members)
            for key in sorted(before - after):
                venue = key.split(":", 1)[0]
                self.pending_settle[key] = MarketMeta(
                    venue=venue,
                    symbol=key.split(":", 1)[1],
                    market_id="",
                    event_id=None,
                    title="",
                    category="other",
                    close_ts=0,
                )
            events.append(
                {
                    "type": "set_change",
                    "ts": now,
                    "added": sorted(after - before),
                    "removed": sorted(before - after),
                    "size": len(after),
                }
            )
        events += await self._settle_pending(now)
        return events

    async def _settle_pending(self, now: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in sorted(self.pending_settle):
            venue, symbol = key.split(":", 1)
            value: float | None = None
            try:
                if venue == "kalshi":
                    r = await self.http.get(f"{self.kalshi}/markets/{symbol}")
                    m = r.data.get("market", {}) if isinstance(r.data, dict) else {}
                    value = {"yes": 1.0, "no": 0.0}.get(str(m.get("result") or "").lower())
                elif self.health["polymarket"].stopped is None:
                    r = await self.http.get(
                        f"{self.gamma}/markets/keyset", {"clob_token_ids": symbol, "closed": "true"}
                    )
                    ms = r.data.get("markets", []) if isinstance(r.data, dict) else []
                    if ms:
                        row = npm.market_row(ms[0], now, self.cats)
                        if row and row["settlement_value"] is not None:
                            value = float(row["settlement_value"])
            except HttpError as exc:
                if venue == "polymarket":
                    self._maybe_stop("polymarket", exc)
                continue
            if value is not None:
                out += self.ledger.settle(now, key, value)
                del self.pending_settle[key]
        return out

    def _maybe_stop(self, venue: str, exc: HttpError) -> None:
        if exc.status in BLOCKED and self.health[venue].stopped is None:
            self.health[venue].stopped = f"HTTP {exc.status} at {datetime.now(UTC).isoformat()}"
            log.error(
                "venue_stopped",
                venue=venue,
                status=exc.status,
                note="reads refused; polling stopped, never retried another way",
            )

    # ------------------------------------------------------------------ polling

    async def poll_kalshi(
        self, members: list[MarketMeta]
    ) -> tuple[dict[str, pl.DataFrame], list[TradePrint]]:
        books: dict[str, pl.DataFrame] = {}
        tickers = [m.symbol for m in members]
        for i in range(0, len(tickers), 100):
            batch = tickers[i : i + 100]
            t0 = time.perf_counter()
            res = await self.http.get(
                f"{self.kalshi}/markets/orderbooks", [("tickers", t) for t in batch]
            )
            self.health["kalshi"].latencies_ms.append((time.perf_counter() - t0) * 1000)
            self._offset(res)
            self._store("kalshi", "rec_books", res, {"tickers": batch})
            for ob in res.data.get("orderbooks", []) if isinstance(res.data, dict) else []:
                rec = {
                    "ts_recv_ns": res.ts_recv_ns,
                    "key": {"ticker": ob.get("ticker")},
                    "body": ob,
                }
                rows = nk.book(rec, 0)
                books[f"kalshi:{ob.get('ticker')}"] = book_frame(rows)
        trades: list[TradePrint] = []
        wanted = set(tickers)
        params: dict[str, str | int] = {"limit": 1000}
        start = self.kalshi_trade_ts or (self.now_ns() // NS - int(self.pcfg.poll_interval_s))
        params["min_ts"] = start
        cursor: str | None = None
        newest = start
        for _ in range(20):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            res = await self.http.get(f"{self.kalshi}/markets/trades", p)
            self._store("kalshi", "rec_trades", res, {"min_ts": start})
            rec = {"ts_recv_ns": res.ts_recv_ns, "body": res.data, "key": {"source": "live"}}
            for t in nk.trades(rec):
                tid = str(t["trade_id"])
                if tid in self.seen_set:
                    continue
                self._remember(tid)
                newest = max(newest, int(t["ts_exchange"] or 0) // NS)
                if t["symbol"] in wanted:
                    trades.append(
                        TradePrint(
                            "kalshi",
                            str(t["symbol"]),
                            int(t["ts_exchange"]),
                            float(t["price"]),
                            float(t["qty"]),
                            int(t["side"]),
                        )
                    )
            cursor = res.data.get("cursor") if isinstance(res.data, dict) else None
            if not cursor:
                break
        self.kalshi_trade_ts = newest
        return books, trades

    def _remember(self, tid: str) -> None:
        if len(self.seen_trades) == self.seen_trades.maxlen:
            self.seen_set.discard(self.seen_trades[0])
        self.seen_trades.append(tid)
        self.seen_set.add(tid)

    def _offset(self, res: FetchResult) -> None:
        off = res.clock_offset_s
        if off is not None:
            self.offsets.append(off)

    async def poll_polymarket(
        self, members: list[MarketMeta]
    ) -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
        books: dict[str, pl.DataFrame] = {}
        no_books: dict[str, pl.DataFrame] = {}

        async def one(m: MarketMeta, token: str, no: bool) -> None:
            t0 = time.perf_counter()
            res = await self.http.get(f"{self.clob}/book", {"token_id": token})
            self.health["polymarket"].latencies_ms.append((time.perf_counter() - t0) * 1000)
            self._offset(res)
            self._store(
                "polymarket", "rec_books", res, {"token_id": token, "yes_token": m.symbol, "no": no}
            )
            frame = book_frame(
                npm.book(
                    {"ts_recv_ns": res.ts_recv_ns, "body": res.data, "key": {"token_id": token}}, 0
                )
            )
            (no_books if no else books)[m.key] = frame

        jobs = [one(m, m.symbol, False) for m in members]
        if self.pcfg.markets.polymarket_no_books:
            jobs += [one(m, m.no_symbol, True) for m in members if m.no_symbol]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        for e in errors:
            if isinstance(e, HttpError):
                self._maybe_stop("polymarket", e)
        if errors and len(errors) == len(results):
            raise errors[0]
        return books, no_books

    # ------------------------------------------------------------------ cycle

    async def cycle(self) -> dict[str, Any]:
        """One poll of every venue plus the paper strategies; returns the heartbeat."""
        events = await self.refresh()
        now = self.now_ns()
        members = list(self.set.members.values())
        kal = [m for m in members if m.venue == "kalshi"]
        pm = [m for m in members if m.venue == "polymarket"]
        books: dict[str, pl.DataFrame] = {}
        no_books: dict[str, pl.DataFrame] = {}
        trades: list[TradePrint] = []
        limit = 3 * self.pcfg.poll_interval_s * NS
        self.offsets = []

        async def run_kalshi() -> None:
            b, t = await self.poll_kalshi(kal)
            books.update(b)
            trades.extend(t)

        async def run_pm() -> None:
            b, n = await self.poll_polymarket(pm)
            books.update(b)
            no_books.update(n)

        jobs: dict[str, Any] = {}
        if kal:
            jobs["kalshi"] = run_kalshi()
        if pm and self.health["polymarket"].stopped is None:
            jobs["polymarket"] = run_pm()
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for venue, res in zip(jobs, results, strict=True):
            h = self.health[venue]
            if isinstance(res, Exception):
                h.failures += 1
                log.warning("poll_failed", venue=venue, error=f"{type(res).__name__}: {res}")
                continue
            if h.last_success_ns is not None and now - h.last_success_ns > limit:
                self._gap(venue, h.last_success_ns, now, "no successful poll")
            h.last_success_ns = now
        self.cycle_n += 1
        if self.cycle_n % self.pcfg.flush_every_cycles == 0:
            for w in self.writers.values():
                w.flush()
        if self.paper:
            meta = {m.key: m for m in members}
            c = Cycle(ts=now, books=books, no_books=no_books, trades=trades, meta=meta)
            for s in self.strategies:
                events += s.on_cycle(c)
            mids = {k: v for k, b in books.items() if (v := mid(b)) is not None}
            events += self.ledger.marks(now, mids)
            self.ledger.write(events)
        return self.heartbeat(now, len(members), len(trades))

    def _disk(self) -> dict[str, Any]:
        now = self.clock()
        if now - self.last_disk_check >= self.pcfg.disk_check_minutes * 60:
            self.last_disk_check = now
            u = shutil.disk_usage(self.data_dir)
            used = 100.0 * u.used / u.total
            self.disk_warn = used >= self.pcfg.disk_warn_used_pct
            self._disk_info = {
                "data_fs_used_pct": round(used, 1),
                "data_fs_free_gb": round(u.free / 1e9, 1),
            }
            if Path("/mnt/d").exists():
                try:
                    d = shutil.disk_usage("/mnt/d")
                    self._disk_info["d_drive_free_gb"] = round(d.free / 1e9, 1)
                    self.disk_warn = self.disk_warn or d.free < 150e9
                except OSError:
                    pass
            if self.disk_warn:
                log.warning("disk_warning", **self._disk_info)
        return {**getattr(self, "_disk_info", {}), "disk_warn": self.disk_warn}

    def heartbeat(self, now: int, n_markets: int, n_trades: int) -> dict[str, Any]:
        lat = {
            v: (
                statistics.median(h.latencies_ms) if h.latencies_ms else None,
                (
                    sorted(h.latencies_ms)[int(0.95 * (len(h.latencies_ms) - 1))]
                    if h.latencies_ms
                    else None
                ),
            )
            for v, h in self.health.items()
        }
        offs = sorted(self.offsets)
        hb = {
            "ts_ns": now,
            "ts": datetime.fromtimestamp(now / NS, tz=UTC).isoformat(),
            "cycle": self.cycle_n,
            "markets": n_markets,
            "trades_in_cycle": n_trades,
            "last_success_ns": {v: h.last_success_ns for v, h in self.health.items()},
            "failures": {v: h.failures for v, h in self.health.items()},
            "stopped": {v: h.stopped for v, h in self.health.items()},
            "latency_ms_p50_p95": lat,
            "clock_offset_s_median": offs[len(offs) // 2] if offs else None,
            "clock_offset_flag": bool(offs and abs(offs[len(offs) // 2]) > 2.0),
            **self._disk(),
        }
        tmp = self.dir / "heartbeat.json.tmp"
        tmp.write_text(json.dumps(hb, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.dir / "heartbeat.json")
        log.info("cycle", **{k: v for k, v in hb.items() if k != "last_success_ns"})
        return hb

    def period_over(self) -> bool:
        """True once the planned paper period has ended (writes ``paper/STOP``)."""
        if self.pcfg.period is None:
            return False
        end = datetime.fromisoformat(self.pcfg.period.end.replace("Z", "+00:00")).timestamp()
        if self.clock() < end:
            return False
        stop = self.dir / "STOP"
        if not stop.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            stop.write_text(
                f"paper period ended at {self.pcfg.period.end}" + chr(10), encoding="utf-8"
            )
            log.info("period_over", end=self.pcfg.period.end)
        return True

    async def run(self, duration_s: float | None = None) -> None:
        """Poll until ``duration_s`` elapses (forever when None) or the paper period ends."""
        self._start_bookkeeping()
        start = self.clock()
        interval = self.pcfg.poll_interval_s
        try:
            while (duration_s is None or self.clock() - start < duration_s) and (
                not self.paper or not self.period_over()
            ):
                t0 = self.clock()
                try:
                    await self.cycle()
                except Exception as exc:  # noqa: BLE001 - the loop must survive any one cycle
                    log.error("cycle_failed", error=f"{type(exc).__name__}: {exc}")
                wait = interval - ((self.clock() - t0) % interval)
                await asyncio.sleep(wait)
        finally:
            for w in self.writers.values():
                w.flush()
            await self.http.aclose()


def latest_dtb3(data_dir: Path) -> float:
    """Latest DTB3 rate (decimal) from the normalized FRED data; 0 when none is ingested."""
    from pmr.normalize.store import read_interim

    rf = read_interim(data_dir, "risk_free")
    if rf.is_empty():
        log.warning("no_dtb3", note="run pmr ingest fred and pmr normalize; lockup uses r = 0")
        return 0.0
    return float(rf.sort("date")["rate_pct"][-1]) / 100.0


def config_hash(*paths: Path) -> str:
    """Short hash of the config files (stamped on every paper event)."""
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:16]


def status(data_dir: Path, pcfg: PaperConfig, now_ns: int | None = None) -> dict[str, Any]:
    """``pmr paper status``: uptime per venue since the planned start (or first start)."""
    d = Path(data_dir) / "paper"
    hb = json.loads((d / "heartbeat.json").read_text()) if (d / "heartbeat.json").exists() else {}
    run = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else {}
    now = now_ns or int(time.time() * NS)
    start = int(run.get("first_start_ns", now))
    if pcfg.period is not None:
        p0 = int(datetime.fromisoformat(pcfg.period.start.replace("Z", "+00:00")).timestamp())
        if now >= p0 * NS:
            start = max(start, p0 * NS)
    gaps: list[dict[str, str]] = []
    if (d / "gaps.csv").exists():
        with (d / "gaps.csv").open(encoding="utf-8") as fh:
            gaps = list(csv.DictReader(fh))
    span = max(1, now - start)
    uptime: dict[str, float] = {}
    for venue in ("kalshi", "polymarket"):
        # Merge overlapping gap rows so one outage is never counted twice.
        spans = sorted(
            (max(int(g["start_ts"]), start), min(int(g["end_ts"]), now))
            for g in gaps
            if g["venue"] == venue
        )
        down = 0
        covered = 0
        cur_a = cur_b = None
        for a, b in spans:
            if b <= a:
                continue
            if cur_b is None or a > cur_b:
                if cur_b is not None and cur_a is not None:
                    down += cur_b - cur_a
                cur_a, cur_b = a, b
            else:
                cur_b = max(cur_b, b)
        if cur_a is not None and cur_b is not None:
            down += cur_b - cur_a
        for g in gaps:
            if g["venue"] == venue:
                covered = max(covered, int(g["end_ts"]))
        last = max(int((hb.get("last_success_ns") or {}).get(venue) or 0), covered)
        if last and now - last > 3 * pcfg.poll_interval_s * NS:
            down += now - max(last, start)  # an open gap not yet in gaps.csv
        uptime[venue] = round(100.0 * (1 - down / span), 3)
    return {
        "since": datetime.fromtimestamp(start / NS, tz=UTC).isoformat(),
        "hours": round(span / NS / 3600, 2),
        "uptime_pct": uptime,
        "gaps": len(gaps),
        "last_heartbeat": hb.get("ts"),
        "stopped": hb.get("stopped"),
        "disk_warn": hb.get("disk_warn"),
        "markets": hb.get("markets"),
    }
