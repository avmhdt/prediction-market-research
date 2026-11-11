"""FR-23 / FR-24 / DQ-14: recorder, market set, strategies and ledger against synthetic venues."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from pmr.fees.table import FeeTable
from pmr.http.client import HttpClient
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.normalize import categories
from pmr.paper import recorder as rec_mod
from pmr.paper.config import PaperConfig, load_paper_config
from pmr.paper.market_set import MarketMeta, MarketSet
from pmr.paper.recorder import Recorder, book_frame, mid, status
from pmr.paper.strategies import ArbMonitor, Cycle, FLBTilt, MMQuotes, TradePrint
from pmr.paper.trader import Ledger, restore
from pmr.settings import PmrConfig, Settings
from pmr.sim.mm import MMParams
from tests.conftest import REPO

K = "https://external-api.kalshi.com/trade-api/v2"
G = "https://gamma-api.polymarket.com"
C = "https://clob.polymarket.com"
T0 = 1_795_000_000.0  # seconds, 2026-11-18
NS = 10**9
FEES = FeeTable.load(REPO / "config" / "fees.yaml")


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def pcfg(**kw: Any) -> PaperConfig:
    base = load_paper_config(REPO / "config" / "paper.yaml")
    data = base.model_dump()
    data["markets"].update({"per_venue": {"kalshi": 2, "polymarket": 1}, "max_markets": 10})
    data.update(kw)
    return PaperConfig.model_validate(data)


def kalshi_market(t: str, ev: str, vol: str) -> dict[str, Any]:
    return {
        "ticker": t,
        "event_ticker": ev,
        "title": t,
        "status": "active",
        "close_time": "2026-12-30T00:00:00Z",
        "volume_24h_fp": vol,
    }


def mock_venues(*, pm_status: int = 200, settled: bool = False) -> dict[str, respx.Route]:
    routes: dict[str, respx.Route] = {}
    respx.get(f"{K}/series").mock(
        return_value=httpx.Response(
            200,
            json={
                "series": [
                    {
                        "ticker": "KXEV",
                        "category": "Politics",
                        "fee_type": "quadratic",
                        "fee_multiplier": 1,
                    },
                    {
                        "ticker": "KXMK",
                        "category": "Economics",
                        "fee_type": "quadratic_with_maker_fees",
                        "fee_multiplier": 1,
                    },
                ]
            },
        )
    )

    def markets(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(
                200, json={"markets": [kalshi_market("KXMK-1", "KXMK-E", "5")], "cursor": ""}
            )
        return httpx.Response(
            200,
            json={
                "markets": [
                    kalshi_market("KXEV-A", "KXEV-E", "900"),
                    kalshi_market("KXEV-B", "KXEV-E", "800"),
                ],
                "cursor": "c2",
            },
        )

    respx.get(f"{K}/markets").mock(side_effect=markets)
    respx.get(f"{K}/events/KXEV-E").mock(
        return_value=httpx.Response(
            200,
            json={
                "event": {
                    "event_ticker": "KXEV-E",
                    "series_ticker": "KXEV",
                    "mutually_exclusive": True,
                    "markets": [{"status": "active"}, {"status": "active"}],
                }
            },
        )
    )
    respx.get(f"{K}/events/KXMK-E").mock(
        return_value=httpx.Response(
            200, json={"event": {"event_ticker": "KXMK-E", "series_ticker": "KXMK"}}
        )
    )
    # Both outcomes' YES asks sum to 0.90: a complete multi-outcome set below 1.
    books = {
        "KXEV-A": {"yes_dollars": [["0.4000", "50"]], "no_dollars": [["0.5500", "50"]]},
        "KXEV-B": {"yes_dollars": [["0.4300", "50"]], "no_dollars": [["0.5500", "50"]]},
    }
    routes["books"] = respx.get(f"{K}/markets/orderbooks").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json={
                "orderbooks": [
                    {"ticker": t, "orderbook_fp": books.get(t, {})}
                    for t in r.url.params.get_list("tickers")
                ]
            },
        )
    )
    routes["trades"] = respx.get(f"{K}/markets/trades").mock(
        return_value=httpx.Response(
            200,
            json={
                "trades": [
                    {
                        "trade_id": "t1",
                        "ticker": "KXEV-A",
                        "count_fp": "30",
                        "yes_price_dollars": "0.3500",
                        "taker_side": "no",
                        "created_time": "2026-11-18T00:00:10Z",
                    },
                    {
                        "trade_id": "t2",
                        "ticker": "OTHER",
                        "count_fp": "1",
                        "yes_price_dollars": "0.5",
                        "taker_side": "yes",
                        "created_time": "2026-11-18T00:00:11Z",
                    },
                ],
                "cursor": "",
            },
        )
    )
    respx.get(url__regex=rf"{K}/markets/KXEV-[AB]$").mock(
        return_value=httpx.Response(200, json={"market": {"result": "yes" if settled else ""}})
    )
    pm_market = {
        "conditionId": "0xpm",
        "question": "PM?",
        "clobTokenIds": '["y1", "n1"]',
        "endDate": "2026-12-31T00:00:00Z",
        "acceptingOrders": True,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.04},
        "orderPriceMinTickSize": 0.01,
        "volume24hr": 1e6,
        "tags": [{"label": "Politics"}],
        "events": [{"id": "77", "negRisk": False}],
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "closed": True,
    }
    soon = {**pm_market, "clobTokenIds": '["y2", "n2"]', "endDate": "2026-11-18T06:00:00Z"}
    routes["gamma"] = respx.get(f"{G}/markets/keyset").mock(
        side_effect=lambda r: (
            httpx.Response(pm_status, json={"markets": [soon, pm_market]})
            if r.url.params.get("closed") == "false"
            else httpx.Response(200, json={"markets": [pm_market]})
        )
    )
    pm_books = {
        "y1": {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        },
        "n1": {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
        },
    }
    routes["clob"] = respx.get(f"{C}/book").mock(
        side_effect=lambda r: httpx.Response(
            pm_status,
            json={
                "asset_id": r.url.params["token_id"],
                "timestamp": "1795000000000",
                **pm_books[r.url.params["token_id"]],
            },
        )
    )
    return routes


def recorder(
    tmp_path: Path, settings: Settings, cfg: PmrConfig, clock: Clock, p: PaperConfig | None = None
) -> Recorder:
    http = HttpClient(settings, cfg.http, limits=RateLimiterRegistry(default_rps=10_000))
    return Recorder(
        settings,
        cfg,
        p or pcfg(),
        tmp_path,
        categories.load(str(REPO / "config" / "categories.yaml")),
        FEES,
        http=http,
        clock=clock,
        config_hash="h",
    )


@respx.mock
async def test_recorder_cycles_strategies_gaps_and_restart(
    tmp_path: Path, settings: Settings, cfg: PmrConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = mock_venues()
    clock = Clock()
    rec = recorder(tmp_path, settings, cfg, clock)
    rec._start_bookkeeping()
    hb = await rec.cycle()
    assert hb["markets"] == 3 and hb["failures"] == {"kalshi": 0, "polymarket": 0}
    assert sorted(rec.set.members) == ["kalshi:KXEV-A", "kalshi:KXEV-B", "polymarket:y1"]
    assert rec.set.members["kalshi:KXEV-A"].event_size == 2
    assert routes["clob"].call_count == 2  # YES and NO books
    assert hb["trades_in_cycle"] == 1
    clock.t += 30
    await rec.cycle()
    clock.t += 200  # longer than 3 polling intervals without a poll: a gap
    await rec.cycle()
    for w in rec.writers.values():
        w.flush()
    gaps = (tmp_path / "paper" / "gaps.csv").read_text().splitlines()
    assert gaps[0].startswith("venue,") and len(gaps) == 3
    events = [
        json.loads(x)
        for x in (tmp_path / "paper").glob("paper-*.jsonl").__next__().read_text().splitlines()
    ]
    kinds = {e["type"] for e in events}
    assert {"set_change", "opportunity", "fill", "order_intent", "gap"} <= kinds
    arb = [e for e in events if e["type"] == "fill" and e["strategy"] == "arb_monitor"]
    assert {e["symbol"] for e in arb} >= {"KXEV-A", "KXEV-B"}
    assert all(e["config_hash"] == "h" for e in events)
    # Restart: positions come back from the log, downtime becomes a gap.
    clock.t += 600
    rec2 = recorder(tmp_path, settings, cfg, clock)
    assert rec2.ledger.positions == rec.ledger.positions
    rec2._start_bookkeeping()
    assert "downtime" in (tmp_path / "paper" / "gaps.csv").read_text()
    st = status(tmp_path, pcfg(period=None), now_ns=int(clock.t * NS))
    assert st["markets"] == 3 and st["gaps"] >= 3 and 0 < st["uptime_pct"]["kalshi"] < 100

    # run() for a bounded duration with sleeps patched out.
    async def fast_sleep(s: float) -> None:
        clock.t += s

    monkeypatch.setattr(rec_mod.asyncio, "sleep", fast_sleep)
    await rec2.run(duration_s=65)
    assert rec2.cycle_n >= 2


@respx.mock
async def test_settlement_after_a_market_leaves_the_set(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    mock_venues(settled=True)
    clock = Clock()
    rec = recorder(tmp_path, settings, cfg, clock)
    await rec.cycle()
    assert rec.ledger.positions
    now = int((T0 + 25 * 3600) * NS)
    for key in ("kalshi:KXEV-A", "kalshi:KXEV-B", "polymarket:y1"):
        rec.pending_settle[key] = rec.set.members[key]
    out = await rec._settle_pending(now)
    rec.ledger.write(out)
    assert not rec.pending_settle
    settled = [
        json.loads(x)
        for f in (tmp_path / "paper").glob("paper-*.jsonl")
        for x in f.read_text().splitlines()
        if '"settlement"' in x
    ]
    assert settled and all(s["value_per_contract"] == 1.0 for s in settled)


@respx.mock
async def test_polymarket_blocked_stops_polling(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    routes = mock_venues(pm_status=403)
    rec = recorder(tmp_path, settings, cfg, Clock())
    hb = await rec.cycle()
    assert hb["stopped"]["polymarket"] is not None
    calls = routes["gamma"].call_count
    await rec.cycle()
    assert routes["gamma"].call_count == calls and routes["clob"].call_count == 0


async def test_period_over_writes_stop(tmp_path: Path, settings: Settings, cfg: PmrConfig) -> None:
    clock = Clock()
    p = pcfg(
        period={
            "start": "2026-11-15T00:00:00Z",
            "end": "2026-11-17T00:00:00Z",
            "hard_end": "2026-11-20T00:00:00Z",
        }
    )
    rec = recorder(tmp_path, settings, cfg, clock, p)
    assert rec.period_over() and (tmp_path / "paper" / "STOP").exists()
    await rec.run(duration_s=1)  # returns at once
    assert rec.cycle_n == 0


def meta(key: str, **kw: Any) -> MarketMeta:
    venue, symbol = key.split(":")
    base = dict(
        venue=venue,
        symbol=symbol,
        market_id=symbol,
        event_id=None,
        title=symbol,
        category="politics",
        close_ts=int((T0 + 30 * 86400) * NS),
    )
    base.update(kw)
    return MarketMeta(**base)  # type: ignore[arg-type]


def bk(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> pl.DataFrame:
    rows = [{"side": 1, "price": p, "qty": q} for p, q in bids]
    rows += [{"side": -1, "price": p, "qty": q} for p, q in asks]
    return book_frame(rows)


def test_strategies_on_a_synthetic_cycle(tmp_path: Path) -> None:
    from pmr.analysis.arb import ArbConfig
    from pmr.paper.config import ArbCfg, FLBCfg, MMCfg

    ts = int(T0 * NS)
    m_pm = meta("polymarket:y", no_symbol="n", fee_rate=0.04)
    m_fav = meta("kalshi:FAV")
    m_maker = meta("kalshi:MK", fee_type="quadratic_with_maker_fees")
    cyc = Cycle(
        ts=ts,
        books={
            "polymarket:y": bk([(0.40, 100)], [(0.45, 100)]),
            "kalshi:FAV": bk([(0.90, 10)], [(0.93, 200)]),
            "kalshi:MK": bk([(0.40, 10)], [(0.60, 10)]),
        },
        no_books={"polymarket:y": bk([(0.40, 100)], [(0.50, 100)])},
        trades=[TradePrint("kalshi", "FAV", ts + 1, 0.80, 50, -1)],
        meta={m.key: m for m in (m_pm, m_fav, m_maker)},
    )
    arb = ArbMonitor(ArbCfg(), ArbConfig(fees=FEES, r_annual=0.04))
    ev = arb.on_cycle(cyc)
    fills = [e for e in ev if e["type"] == "fill"]
    assert {(e["symbol"], e["side"]) for e in fills} == {("y", 1), ("y", -1)}
    yes_fill = next(e for e in fills if e["side"] == 1)
    assert yes_fill["price"] == 0.45 and yes_fill["fee"] is not None
    no_fill = next(e for e in fills if e["side"] == -1)
    assert no_fill["price"] == pytest.approx(0.50)  # NO bought at 0.50 = YES sold at 0.50
    flb = FLBTilt(FLBCfg(), FEES)
    ev = flb.on_cycle(cyc)
    assert [e["symbol"] for e in ev if e["type"] == "fill"] == ["FAV"]
    assert flb.on_cycle(cyc) == []  # once per market
    mm = MMQuotes(
        MMCfg(),
        MMParams(half_spread=0.02, skew_gamma=0.0, q_max=100, capital_max=1000, ema_halflife_s=300),
        FEES,
    )
    ev = mm.on_cycle(cyc)
    assert "kalshi:MK" not in mm.makers  # maker coefficient unknown: skipped
    cyc2 = Cycle(
        ts=ts + 30 * NS,
        books=cyc.books,
        no_books={},
        meta=cyc.meta,
        trades=[TradePrint("kalshi", "FAV", ts + 31 * NS, 0.80, 50, -1)],
    )
    ev = mm.on_cycle(cyc2)
    assert any(e["type"] == "fill" and e["liquidity"] == "maker" for e in ev)


def test_ledger_marks_settle_restore_and_helpers(tmp_path: Path) -> None:
    led = Ledger(tmp_path, "h")
    ts = int(T0 * NS)
    led.write(
        [
            {
                "type": "fill",
                "ts": ts,
                "strategy": "flb_tilt",
                "venue": "kalshi",
                "symbol": "A",
                "side": 1,
                "price": 0.9,
                "qty": 10,
                "fee": 0.01,
            },
            {
                "type": "fill",
                "ts": ts,
                "strategy": "arb_monitor",
                "venue": "kalshi",
                "symbol": "A",
                "side": -1,
                "price": 0.5,
                "qty": 2,
                "fee": None,
            },
        ]
    )
    marks = led.marks(ts, {"kalshi:A": 0.95})
    assert len(marks) == 2 and led.marks(ts + 1, {}) == []
    led.write(marks)
    settle = led.settle(ts + 2, "kalshi:A", 1.0)
    flb = next(s for s in settle if s["strategy"] == "flb_tilt")
    assert flb["pnl"] == pytest.approx(10 * 1.0 - 9.0 - 0.01)
    arb = next(s for s in settle if s["strategy"] == "arb_monitor")
    assert arb["unknown_fee_fills"] == 1
    led.write(settle)
    again = Ledger(tmp_path, "h")
    done = restore(again)
    assert done == {"kalshi:A"} and again.last_mark_day is not None
    assert all(abs(p.q) < 1e-12 for p in again.positions.values())
    assert mid(bk([(0.4, 1)], [(0.6, 1)])) == pytest.approx(0.5)
    assert mid(bk([(0.6, 1)], [(0.4, 1)])) is None and mid(bk([], [])) is None


def test_market_set_update_and_persistence(tmp_path: Path) -> None:
    s = MarketSet({}, [])
    now = int(T0 * NS)
    cands = {
        "kalshi": [meta("kalshi:A"), meta("kalshi:B"), meta("kalshi:C")],
        "polymarket": [meta("polymarket:x")],
    }
    s.update(cands, {"kalshi": 2, "polymarket": 1}, now, max_markets=10)
    assert sorted(s.members) == ["kalshi:A", "kalshi:B", "polymarket:x"]
    s.members["kalshi:A"] = meta("kalshi:A", close_ts=now - 1)
    cands["kalshi"][0] = s.members["kalshi:A"]  # closed markets are never re-added
    s.update(cands, {"kalshi": 2, "polymarket": 1}, now, max_markets=3)
    assert "kalshi:A" not in s.members and "kalshi:C" in s.members
    path = tmp_path / "ms.json"
    s.save(path)
    back = MarketSet.load(path)
    assert back.members == s.members and back.history == s.history
    assert MarketSet.load(tmp_path / "none.json").members == {}
    assert meta("kalshi:Z", fee_type="quadratic_with_maker_fees").maker_fee_unknown


def test_restore_skips_a_crash_damaged_line(tmp_path: Path) -> None:
    led = Ledger(tmp_path, "h")
    ts = int(T0 * NS)
    led.write(
        [
            {
                "type": "fill",
                "ts": ts,
                "strategy": "mm_quotes",
                "venue": "kalshi",
                "symbol": "A",
                "side": 1,
                "price": 0.5,
                "qty": 4,
                "fee": 0.0,
            }
        ]
    )
    path = next(tmp_path.glob("paper-*.jsonl"))
    with path.open("ab") as fh:
        fh.write(b"\x00" * 3589)  # what the 2026-11-11 crash left behind
    again = Ledger(tmp_path, "h")
    restore(again)
    assert again.corrupt_lines == 1
    assert again.positions[("mm_quotes", "kalshi", "A")].q == 4
    again.write(
        [
            {
                "type": "fill",
                "ts": ts + 1,
                "strategy": "mm_quotes",
                "venue": "kalshi",
                "symbol": "A",
                "side": 1,
                "price": 0.5,
                "qty": 1,
                "fee": 0.0,
            }
        ]
    )
    third = Ledger(tmp_path, "h")
    restore(third)
    assert third.positions[("mm_quotes", "kalshi", "A")].q == 5
