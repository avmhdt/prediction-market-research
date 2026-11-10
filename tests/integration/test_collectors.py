"""AC-W-03 / AC-W-04: every collector against synthetic responses, through raw, normalize and
the warehouse; pagination, resume after a kill, the Kalshi cutoff routing and dedup."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
import respx

from pmr.collectors import manifold as manifold_mod
from pmr.collectors.base import CollectContext, CollectorError, IngestStats
from pmr.collectors.fred import FredCollector
from pmr.collectors.kalshi import KalshiCollector
from pmr.collectors.manifold import ManifoldCollector
from pmr.collectors.metaculus import MetaculusCollector
from pmr.collectors.polymarket_clob import ClobCollector
from pmr.collectors.polymarket_data import DataApiCollector
from pmr.collectors.polymarket_gamma import GammaCollector
from pmr.collectors.polymarket_onchain import OnchainCollector, reconcile
from pmr.collectors.raw import iter_parts, read_part
from pmr.http.client import HttpClient
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.normalize import categories
from pmr.normalize.run import run as normalize
from pmr.normalize.store import current_markets
from pmr.settings import PmrConfig, Settings
from pmr.warehouse.build import build, checksums
from tests.conftest import REPO
from tests.fixtures import synth


async def _nosleep(_: float) -> None:
    return None


def allow_kalshi(cfg: PmrConfig, allowed: bool = True) -> PmrConfig:
    k = cfg.venues.kalshi.model_copy(update={"storage_allowed": allowed})
    return cfg.model_copy(update={"venues": cfg.venues.model_copy(update={"kalshi": k})})


@asynccontextmanager
async def context(
    tmp_path: Path, cfg: PmrConfig, settings: Settings, **kw: Any
) -> AsyncIterator[CollectContext]:
    async with HttpClient(
        settings, cfg.http, limits=RateLimiterRegistry(default_rps=10_000), sleep=_nosleep
    ) as http:
        yield CollectContext(
            http=http,
            settings=settings,
            cfg=cfg,
            data_dir=tmp_path,
            since=date(2023, 1, 1),
            until=None,
            flush_every=kw.get("flush_every", 200),
        )


def cats() -> categories.CategoryMap:
    return categories.load(str(REPO / "config" / "categories.yaml"))


def wh(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    path = tmp_path / "warehouse.duckdb"
    build(tmp_path, path, cats(), threads=1)
    return duckdb.connect(str(path), read_only=True)


def mock_gamma() -> respx.Route:
    def handler(request: httpx.Request) -> httpx.Response:
        assert synth.param(request, "closed") == "true"
        assert synth.param(request, "end_date_min") == "2022-07-05T00:00:00Z"
        return httpx.Response(200, json=synth.gamma_page(synth.param(request, "after_cursor")))

    return respx.get(f"{synth.GAMMA}/markets/keyset").mock(side_effect=handler)


def mock_data_trades() -> respx.Route:
    def handler(request: httpx.Request) -> httpx.Response:
        cid = synth.param(request, "condition") or ""
        return httpx.Response(200, json=synth.v2_trades(cid, synth.param(request, "cursor")))

    return respx.get(f"{synth.DATA}/trades").mock(side_effect=handler)


# ----------------------------------------------------------------------------- Polymarket


@respx.mock
async def test_polymarket_pipeline_and_idempotent_reingest(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    gamma = mock_gamma()
    mock_data_trades()
    respx.get(f"{synth.CLOB}/prices-history").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json={"history": [{"t": synth.T0, "p": 0.5}, {"t": synth.T0 + 3600, "p": 0.55}]},
        )
    )
    async with context(tmp_path, cfg, settings) as ctx:
        st = IngestStats()
        await GammaCollector(ctx).markets(st)
        assert st.pages == 3 and st.items == 5 and gamma.call_count == 3
    normalize(tmp_path, cats())
    m = current_markets(tmp_path, "polymarket")
    assert m.height == 5
    async with context(tmp_path, cfg, settings) as ctx:
        st = IngestStats()
        await DataApiCollector(ctx).trades(m["market_id"].to_list(), st)
        assert st.units_done == 5 and st.items == 30
        rows = [(r["market_id"], r["symbol"], 1_719_000_000) for r in m.iter_rows(named=True)]
        await ClobCollector(ctx).prices(rows[:1], st)
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    res = dict(con.execute("SELECT result, count(*) FROM markets_current GROUP BY 1").fetchall())
    assert res == {"YES": 3, "NO": 2}
    assert con.execute("SELECT count(*) FROM trades").fetchone() == (30,)
    # NO prints become YES prices with the side flipped and the YES symbol filled in.
    no = con.execute(
        "SELECT price, side, symbol FROM trades WHERE trade_id IN "
        "(SELECT trade_id FROM trades WHERE abs(price - 0.65) < 1e-12) LIMIT 1"
    ).fetchone()
    assert no is not None and abs(no[0] - 0.65) < 1e-12 and no[1] == -1 and no[2].endswith("111")
    assert con.execute("SELECT count(*) FROM markets_current WHERE disputed").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM price_history").fetchone()[0] > 0  # type: ignore[index]
    before = checksums(tmp_path / "warehouse.duckdb")
    con.close()

    # A second identical ingest adds raw pages but zero warehouse rows (AC-W-03).
    for ck in (tmp_path / "raw").rglob("_checkpoint*.json"):
        ck.unlink()
    async with context(tmp_path, cfg, settings) as ctx:
        await GammaCollector(ctx).markets(IngestStats())
        await DataApiCollector(ctx).trades(m["market_id"].to_list(), IngestStats())
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    assert con.execute("SELECT count(*) FROM trades").fetchone() == (30,)
    assert con.execute("SELECT count(*) FROM markets_current").fetchone() == (5,)
    con.close()
    after = checksums(tmp_path / "warehouse.duckdb")
    assert before["trades"] == after["trades"]


@respx.mock
async def test_kill_and_resume_has_no_gaps_or_duplicates(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 3:  # dies on the third page
            raise RuntimeError("killed")
        return httpx.Response(
            200, json=synth.gamma_page(synth.param(request, "after_cursor"), per_page=1)
        )

    respx.get(f"{synth.GAMMA}/markets/keyset").mock(side_effect=flaky)
    async with context(tmp_path, cfg, settings, flush_every=1) as ctx:
        with pytest.raises(RuntimeError):
            await GammaCollector(ctx).markets(IngestStats())
    assert len(iter_parts(tmp_path, "polymarket")) == 2
    async with context(tmp_path, cfg, settings, flush_every=1) as ctx:
        st = IngestStats()
        await GammaCollector(ctx).markets(st)
    assert st.pages == 3  # resumed at the third page
    normalize(tmp_path, cats())
    ids = sorted(current_markets(tmp_path, "polymarket")["market_id"].to_list())
    assert ids == [f"0xc{i:03d}" for i in range(5)]
    async with context(tmp_path, cfg, settings) as ctx:  # finished: nothing more to fetch
        st = IngestStats()
        await GammaCollector(ctx).markets(st)
    assert st.pages == 0


@respx.mock
async def test_books_and_onchain_reconciliation(tmp_path: Path, cfg: PmrConfig) -> None:
    respx.get(f"{synth.CLOB}/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "asset_id": "1000111",
                "timestamp": str(synth.T0 * 1000),
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.45", "size": "50"}, {"price": "0.47", "size": "10"}],
            },
        )
    )
    s = Settings(_env_file=None, polymarket_subgraph_url="https://sub.example.org/gn")  # type: ignore[call-arg]
    fills = [{"id": "1", "transactionHash": "0xtx0_0", "timestamp": str(synth.T0)}]
    sub = respx.get("https://sub.example.org/gn").mock(
        return_value=httpx.Response(200, json={"data": {"orderFilledEvents": fills}})
    )
    async with context(tmp_path, cfg, s) as ctx:
        await ClobCollector(ctx).books(["1000111", "1000111"], IngestStats())
        await OnchainCollector(ctx).fills(["1000111"], IngestStats())
    assert "query" in str(sub.calls.last.request.url)
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    bbo = con.execute(
        "SELECT seq, bid_px, bid_qty, ask_px, ask_qty FROM bbo ORDER BY seq"
    ).fetchall()
    assert bbo == [(1, 0.40, 100.0, 0.45, 50.0), (2, 0.40, 100.0, 0.45, 50.0)]
    con.close()

    ok = reconcile("m", [{"tx_hash": "0xA", "qty": 10}], [{"tx_hash": "0xa", "qty": 10.04}])
    assert ok.passed and ok.match_rate == 1.0
    bad = reconcile("m", [{"tx_hash": "0xA", "qty": 10}, {"tx_hash": "0xB", "qty": 1}], [])
    assert not bad.passed and bad.size_diff == 1.0
    assert reconcile("m", [], []).passed


async def test_onchain_needs_a_configured_endpoint(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    async with context(tmp_path, cfg, settings) as ctx:
        with pytest.raises(CollectorError, match="SUBGRAPH"):
            OnchainCollector(ctx)


# --------------------------------------------------------------------------------- Kalshi


async def test_kalshi_refuses_until_storage_is_allowed(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    cfg = allow_kalshi(cfg, allowed=False)
    async with context(tmp_path, cfg, settings) as ctx:
        with pytest.raises(CollectorError, match="owner's ruling"):
            KalshiCollector(ctx)
    assert not (tmp_path / "raw").exists()


@respx.mock
async def test_kalshi_routing_dedup_and_book_conversion(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    cfg = allow_kalshi(cfg)
    k = synth.KALSHI
    respx.get(f"{k}/historical/cutoff").mock(
        return_value=httpx.Response(
            200,
            json={"market_settled_ts": synth.CUTOFF_ISO, "trades_created_ts": synth.CUTOFF_ISO},
        )
    )
    respx.get(f"{k}/series").mock(
        return_value=httpx.Response(
            200, json={"series": [{"ticker": "KXSYN", "title": "Syn", "category": "Economics"}]}
        )
    )
    respx.get(f"{k}/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {"event_ticker": "KXSYN-24JUN", "series_ticker": "KXSYN", "title": "Syn June"}
                ],
                "cursor": "",
            },
        )
    )
    respx.get(f"{k}/markets").mock(
        return_value=httpx.Response(
            200,
            json={
                "markets": [synth.kalshi_market("KXSYN-A", close="2024-06-12T00:00:00Z")],
                "cursor": "",
            },
        )
    )
    respx.get(f"{k}/historical/markets").mock(
        return_value=httpx.Response(
            200, json={"markets": [synth.kalshi_market("KXSYN-B", result="no")], "cursor": ""}
        )
    )

    def trades(source: str) -> Callable[[httpx.Request], httpx.Response]:
        def h(request: httpx.Request) -> httpx.Response:
            if source == "historical":
                assert synth.param(request, "max_ts") == str(synth.CUTOFF)
            else:
                assert synth.param(request, "min_ts") == str(synth.CUTOFF)
            t = synth.param(request, "ticker") or ""
            return httpx.Response(
                200, json=synth.kalshi_trades(t, source, synth.param(request, "cursor"))
            )

        return h

    hist = respx.get(f"{k}/historical/trades").mock(side_effect=trades("historical"))
    live = respx.get(f"{k}/markets/trades").mock(side_effect=trades("live"))
    hc = respx.get(url__regex=rf"{k}/historical/markets/KXSYN-B/candlesticks.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "KXSYN-B",
                "candlesticks": [
                    {
                        "end_period_ts": synth.T0,
                        "price": {"close_dollars": "0.6100"},
                        "yes_bid": {"close": 60},
                        "yes_ask": {"close": 62},
                        "volume": 3,
                    }
                ],
            },
        )
    )
    lc = respx.get(url__regex=rf"{k}/series/KXSYN/markets/KXSYN-A/candlesticks.*").mock(
        return_value=httpx.Response(200, json={"candlesticks": []})
    )
    respx.get(f"{k}/markets/KXSYN-A/orderbook").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderbook": {"yes_dollars": [["0.5500", "10"]], "no_dollars": [["0.4000", "7"]]}
            },
        )
    )
    async with context(tmp_path, cfg, settings) as ctx:
        kc = KalshiCollector(ctx)
        st = IngestStats()
        await kc.series(st)
        await kc.events(st)
        await kc.markets(st)
        await kc.historical_markets(st)
    normalize(tmp_path, cats())
    m = current_markets(tmp_path, "kalshi")
    assert sorted(m["symbol"].to_list()) == ["KXSYN-A", "KXSYN-B"]
    old_open = 1_714_521_600
    async with context(tmp_path, cfg, settings) as ctx:
        kc = KalshiCollector(ctx)
        st = IngestStats()
        # A opens before and closes after the cutoff: both routes. B closed before: historical.
        await kc.trades(
            [("KXSYN-A", old_open, synth.CUTOFF + 86400), ("KXSYN-B", old_open, synth.T0)], st
        )
        await kc.candles(
            [
                ("KXSYN", "KXSYN-A", old_open, old_open + 7200, False),
                ("KXSYN", "KXSYN-B", old_open, old_open + 7200, True),
            ],
            st,
        )
        await kc.books(["KXSYN-A"], st)
    assert hist.call_count == 4 and live.call_count == 2
    assert hc.call_count == 1 and lc.call_count == 1
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    assert con.execute(
        "SELECT symbol, category, series_id FROM markets_current ORDER BY 1"
    ).fetchall() == [("KXSYN-A", "economics", "KXSYN"), ("KXSYN-B", "economics", "KXSYN")]
    assert con.execute("SELECT count(*) FROM trades").fetchone() == (12,)
    assert con.execute("SELECT count(*) FROM trades WHERE source = 'historical'").fetchone() == (8,)
    assert con.execute("SELECT bid_px, bid_qty, ask_px, ask_qty FROM bbo").fetchall() == [
        (0.55, 10.0, 0.6, 7.0)
    ]
    cl = con.execute("SELECT close, yes_bid_close, yes_ask_close FROM price_history").fetchall()
    assert cl == [(0.61, 0.60, 0.62)]
    con.close()


# ------------------------------------------------------------------------ Manifold, others


@respx.mock
async def test_manifold_markets_details_bets(
    tmp_path: Path, cfg: PmrConfig, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manifold_mod, "PAGE", 2)

    def markets(request: httpx.Request) -> httpx.Response:
        before = synth.param(request, "before")
        ids = list(range(6))
        start = 0 if before is None else int(before.removeprefix("mf")) + 1
        return httpx.Response(200, json=[synth.manifold_lite(i) for i in ids[start : start + 2]])

    respx.get(f"{synth.MANIFOLD}/markets").mock(side_effect=markets)
    respx.get(url__regex=rf"{synth.MANIFOLD}/market/mf\d+").mock(
        side_effect=lambda r: httpx.Response(
            200, json=synth.manifold_full(int(r.url.path.rsplit("mf", 1)[1]))
        )
    )
    respx.get(f"{synth.MANIFOLD}/bets").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=synth.manifold_bets(synth.param(r, "contractId") or "", synth.param(r, "before")),
        )
    )
    async with context(tmp_path, cfg, settings) as ctx:
        st = IngestStats()
        await ManifoldCollector(ctx).markets(st)
        assert st.items == 6
    normalize(tmp_path, cats())
    m = current_markets(tmp_path, "manifold")
    assert m.height == 5  # the multiple-choice market is dropped
    eligible = m.filter(m["n_traders"] >= 10)["market_id"].to_list()
    async with context(tmp_path, cfg, settings) as ctx:
        mc = ManifoldCollector(ctx)
        await mc.details(eligible, IngestStats())
        await mc.bets(eligible, IngestStats())
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    rows = dict(
        con.execute(
            "SELECT market_id, result FROM markets_current WHERE venue = 'manifold'"
        ).fetchall()
    )
    assert rows == {"mf0": "YES", "mf1": "NO", "mf2": "VOID", "mf4": "SCALAR", "mf5": "YES"}
    assert con.execute(
        "SELECT count(*) FROM markets_current WHERE category = 'politics' AND is_play_money"
    ).fetchone() == (4,)
    # 4 bets per market, the redemption skipped: 3 trades each for 4 markets.
    assert con.execute("SELECT count(*) FROM trades").fetchone() == (12,)
    sold = con.execute("SELECT price, side FROM trades WHERE trade_id = 'mf0-b2'").fetchone()
    assert sold == (0.625, -1)
    assert con.execute("SELECT count(*) FROM price_history WHERE interval_s = 0").fetchone() == (
        12,
    )  # redemptions are skipped for ticks too
    con.close()


@respx.mock
async def test_metaculus_needs_token_then_paginates(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    async with context(tmp_path, cfg, settings) as ctx:
        with pytest.raises(CollectorError, match="METACULUS_API_TOKEN"):
            MetaculusCollector(ctx)
    route = respx.get(f"{synth.METACULUS}/posts/").mock(
        side_effect=lambda r: httpx.Response(
            200, json=synth.metaculus_page(int(synth.param(r, "offset") or 0))
        )
    )
    s = Settings(_env_file=None, metaculus_api_token="tok")  # type: ignore[call-arg]
    async with context(tmp_path, cfg, s) as ctx:
        st = IngestStats()
        await MetaculusCollector(ctx).posts(st)
    assert route.call_count == 2 and st.items == 2
    assert route.calls.last.request.headers["Authorization"] == "Token tok"
    for part in iter_parts(tmp_path, "metaculus"):
        for rec in read_part(part):
            assert "tok" not in str(rec["params"]) and "tok" not in str(rec.get("url"))
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    assert con.execute(
        "SELECT symbol, result, is_forecast FROM markets_current ORDER BY 1"
    ).fetchall() == [("100", "YES", True), ("101", "VOID", True)]
    assert con.execute(
        "SELECT close, n_forecasters FROM price_history WHERE symbol = '100' ORDER BY ts"
    ).fetchall() == [(0.7, 30), (0.75, 31)]
    con.close()


@respx.mock
async def test_fred_and_deterministic_rebuild(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    respx.get(synth.FRED).mock(return_value=httpx.Response(200, text=synth.FRED_CSV))
    mock_gamma()
    async with context(tmp_path, cfg, settings) as ctx:
        await FredCollector(ctx).dtb3(IngestStats())
        await GammaCollector(ctx).markets(IngestStats())
    normalize(tmp_path, cats())
    con = wh(tmp_path)
    assert con.execute('SELECT "date", rate_pct FROM risk_free ORDER BY 1').fetchall() == [
        (date(2024, 6, 3), 5.25),
        (date(2024, 6, 5), 5.24),
    ]
    con.close()
    first = checksums(tmp_path / "warehouse.duckdb")
    normalize(tmp_path, cats(), rebuild=True)
    build(tmp_path, tmp_path / "warehouse.duckdb", cats(), threads=2)
    assert checksums(tmp_path / "warehouse.duckdb") == first
