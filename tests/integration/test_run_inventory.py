"""The ``pmr ingest`` runners end to end (synthetic responses), then ``pmr inventory``."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from pmr.collectors import manifold as manifold_mod
from pmr.collectors import run
from pmr.collectors.base import CollectorError
from pmr.normalize import categories
from pmr.normalize.run import run as normalize
from pmr.settings import PmrConfig, Settings
from pmr.warehouse.build import build
from pmr.warehouse.inventory import inventory
from tests.conftest import REPO
from tests.fixtures import synth
from tests.integration.test_collectors import allow_kalshi, mock_data_trades, mock_gamma


def cats() -> categories.CategoryMap:
    return categories.load(str(REPO / "config" / "categories.yaml"))


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nosleep(_: float) -> None:
        return None

    monkeypatch.setattr("pmr.http.client.asyncio.sleep", nosleep)
    monkeypatch.setattr(manifold_mod, "PAGE", 2)


@respx.mock
async def test_runners_build_and_inventory(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    lo = date(2023, 1, 1)
    mock_gamma()
    mock_data_trades()
    respx.get(f"{synth.CLOB}/prices-history").mock(
        return_value=httpx.Response(200, json={"history": [{"t": synth.T0, "p": 0.5}]})
    )
    respx.get(synth.FRED).mock(return_value=httpx.Response(200, text=synth.FRED_CSV))
    respx.get(f"{synth.MANIFOLD}/markets").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=[synth.manifold_lite(i) for i in range(2)] if not synth.param(r, "before") else [],
        )
    )
    respx.get(url__regex=rf"{synth.MANIFOLD}/market/mf\d+").mock(
        side_effect=lambda r: httpx.Response(
            200, json=synth.manifold_full(int(r.url.path.rsplit("mf", 1)[1]))
        )
    )
    respx.get(f"{synth.MANIFOLD}/bets").mock(return_value=httpx.Response(200, json=[]))

    st = await run.ingest_polymarket(settings, cfg, tmp_path, "gamma", since=lo, until=None)
    assert st.items == 5
    normalize(tmp_path, cats())
    st = await run.ingest_polymarket(settings, cfg, tmp_path, "data", since=lo, until=None)
    assert st.units_done == 5
    st = await run.ingest_polymarket(
        settings, cfg, tmp_path, "clob", since=lo, until=date(2024, 6, 30)
    )
    assert st.units_done == 5
    with pytest.raises(CollectorError):
        await run.ingest_polymarket(settings, cfg, tmp_path, "nope", since=lo, until=None)
    await run.ingest_fred(settings, cfg, tmp_path)
    for what in ("markets",):
        await run.ingest_manifold(settings, cfg, tmp_path, what, since=lo, until=None)
    normalize(tmp_path, cats())
    for what in ("details", "bets"):
        await run.ingest_manifold(settings, cfg, tmp_path, what, since=lo, until=None)
    with pytest.raises(CollectorError):
        await run.ingest_manifold(settings, cfg, tmp_path, "nope", since=lo, until=None)
    with pytest.raises(CollectorError, match="METACULUS"):
        await run.ingest_metaculus(settings, cfg, tmp_path, since=lo, until=None)
    with pytest.raises(CollectorError, match="owner's ruling"):
        await run.ingest_kalshi(
            settings, cfg, tmp_path, "markets", since=lo, until=None, status=None
        )
    normalize(tmp_path, cats())

    wh = tmp_path / "w.duckdb"
    build(tmp_path, wh, cats(), threads=1)
    res = inventory(wh, tmp_path, since=lo, until=None, min_trades=5, min_notional=30.0)
    # Each synthetic Polymarket market has 6 trades and about USD 38 of notional.
    assert res.eligible_by_venue.get("polymarket") == 5
    assert res.eligible_total == 5 and not res.meets_m2_exit
    assert res.other_share == 0.0
    md = res.markdown
    assert "Total eligible real-money markets: **5**" in md
    assert "| manifold | resolved |" in md and "resolved after a dispute" in md
    assert "DTB3" not in md  # counts only, no series values
    strict = inventory(wh, tmp_path, since=lo, until=None, min_trades=5, min_notional=100.0)
    assert strict.eligible_total == 0


@respx.mock
async def test_kalshi_runner_routes_every_what(
    tmp_path: Path, cfg: PmrConfig, settings: Settings
) -> None:
    cfg = allow_kalshi(cfg)
    k = synth.KALSHI
    respx.get(f"{k}/historical/cutoff").mock(
        return_value=httpx.Response(
            200, json={"market_settled_ts": synth.CUTOFF_ISO, "trades_created_ts": synth.CUTOFF_ISO}
        )
    )
    respx.get(f"{k}/series").mock(return_value=httpx.Response(200, json={"series": []}))
    respx.get(f"{k}/events").mock(return_value=httpx.Response(200, json={"events": []}))
    respx.get(f"{k}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [synth.kalshi_market("KXSYN-A")]})
    )
    respx.get(f"{k}/historical/markets").mock(
        return_value=httpx.Response(200, json={"markets": []})
    )
    respx.get(f"{k}/historical/trades").mock(return_value=httpx.Response(200, json={"trades": []}))
    respx.get(f"{k}/markets/trades").mock(return_value=httpx.Response(200, json={"trades": []}))
    respx.get(url__regex=rf"{k}/historical/markets/KXSYN-A/candlesticks.*").mock(
        return_value=httpx.Response(200, json={"candlesticks": []})
    )
    respx.get(f"{k}/markets/KXSYN-A/orderbook").mock(
        return_value=httpx.Response(200, json={"orderbook": {"yes": [[40, 1]], "no": []}})
    )
    lo = date(2023, 1, 1)
    for what in ("series", "events", "markets", "historical"):
        await run.ingest_kalshi(settings, cfg, tmp_path, what, since=lo, until=None, status=None)
    normalize(tmp_path, cats())
    for what in ("trades", "candles", "books"):
        st = await run.ingest_kalshi(
            settings, cfg, tmp_path, what, since=lo, until=date(2024, 12, 31), status=None
        )
        assert st.pages >= 1, what
    with pytest.raises(CollectorError):
        await run.ingest_kalshi(settings, cfg, tmp_path, "nope", since=lo, until=None, status=None)
