"""``pmr smoke`` against synthetic responses shaped like the venue docs (values invented)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pmr import cli
from pmr.settings import PmrConfig, Settings
from pmr.smoke import (
    FRED_DTB3_URL,
    POLYMARKET_GEOBLOCK_URL,
    ProbeResult,
    default_probes,
    exit_code,
    format_report,
    run_probes,
)

DATE = {"Date": "Mon, 09 Nov 2026 12:00:00 GMT"}


def mock_all(cfg: PmrConfig, *, data_status: int = 200) -> None:
    k = cfg.venues.kalshi.base_url
    respx.get(f"{k}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [{"ticker": "KXTEST-1"}]}, headers=DATE)
    )
    respx.get(f"{k}/markets/KXTEST-1/orderbook").mock(
        return_value=httpx.Response(
            200, json={"orderbook": {"yes": [[40, 10]], "no": [[55, 5], [50, 1]]}}
        )
    )
    respx.get(f"{k}/markets/trades").mock(
        return_value=httpx.Response(200, json={"trades": [{"trade_id": "t-1"}], "cursor": ""})
    )
    respx.get(f"{k}/historical/cutoff").mock(
        return_value=httpx.Response(200, json={"market_settled_ts": "2026-09-10T00:00:00Z"})
    )
    pm = cfg.venues.polymarket
    respx.get(f"{pm.gamma_url}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"conditionId": "0xnone", "clobTokenIds": None},
                {"conditionId": "0xabc", "clobTokenIds": '["123456789012345678", "9"]'},
            ],
        )
    )
    respx.get(f"{pm.clob_url}/book").mock(
        return_value=httpx.Response(200, json={"bids": [{"price": "0.4"}], "asks": []})
    )
    respx.get(f"{pm.data_url}/trades").mock(
        return_value=httpx.Response(
            data_status, json={"data": [{"transaction_hash": "0x" + "ab" * 32}]}
        )
    )
    respx.get(POLYMARKET_GEOBLOCK_URL).mock(
        return_value=httpx.Response(
            200, json={"blocked": False, "ip": "203.0.113.9", "country": "KY"}
        )
    )
    respx.get(f"{cfg.venues.manifold.base_url}/markets").mock(
        return_value=httpx.Response(200, json=[{"id": "mf1"}])
    )
    respx.get(f"{cfg.venues.metaculus.base_url}/posts/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 77}]})
    )
    respx.get(FRED_DTB3_URL.split("?")[0]).mock(
        return_value=httpx.Response(200, text="observation_date,DTB3\n2026-11-06,3.90\n")
    )


@respx.mock
async def test_all_probes_pass(settings: Settings, cfg: PmrConfig) -> None:
    mock_all(cfg)
    results = await run_probes(settings, cfg)
    by = {(r.source, r.endpoint): r for r in results}
    assert exit_code(results) == 0
    assert by[("kalshi", "GET /markets")].record_id == "KXTEST-1"
    assert by[("kalshi", "GET /markets/{ticker}/orderbook")].note == "3 price levels"
    assert by[("polymarket-gamma", "GET /markets")].record_id == "0xabc"
    assert by[("polymarket-clob", "GET /book")].record_id.startswith("123456789012")
    assert by[("polymarket-data", "GET /trades")].record_id.endswith("...")
    assert by[("metaculus", "GET /posts/")].status == "skipped"
    assert by[("fred", "GET fredgraph.csv?id=DTB3")].record_id == "2026-11-06"
    report = format_report(results, owner_country="KY")
    assert "required probes failed: 0" in report
    assert "203.0.113.9" not in report  # the geoblock IP is never kept
    assert "blocked=False country=KY" in report


@respx.mock
async def test_metaculus_runs_with_token(cfg: PmrConfig) -> None:
    mock_all(cfg)
    s = Settings(_env_file=None, metaculus_api_token="tok")  # type: ignore[call-arg]
    results = await run_probes(s, cfg, sources=["metaculus"])
    assert [r.record_id for r in results] == ["77"]
    assert respx.calls.last.request.headers["Authorization"] == "Token tok"


@respx.mock
async def test_required_failure_sets_exit_code(settings: Settings, cfg: PmrConfig) -> None:
    mock_all(cfg, data_status=410)
    results = await run_probes(settings, cfg, sources=["polymarket-data", "fred"])
    assert len(results) == 2
    assert exit_code(results) == 1
    report = format_report(results, owner_country="KY")
    assert "FAIL" in report and "410" in report


@respx.mock
async def test_dependent_probe_skipped_and_fails(settings: Settings, cfg: PmrConfig) -> None:
    respx.get(f"{cfg.venues.kalshi.base_url}/markets").mock(
        return_value=httpx.Response(200, json={"markets": []})
    )
    results = await run_probes(settings, cfg, probes=default_probes()[:2])
    ob = next(r for r in results if "orderbook" in r.endpoint)
    assert ob.status == "skipped" and not ob.ok and ob.required


def test_report_optional_failure_does_not_fail() -> None:
    r = ProbeResult("polymarket", "GET /api/geoblock", False, False, "403", 1.0, "", "x")
    assert exit_code([r]) == 0
    assert "(optional, not ok)" in format_report([r], owner_country="KY")


@respx.mock
def test_cli_smoke_writes_report(
    tmp_path: Path, cfg: PmrConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_all(cfg)
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    out = tmp_path / "smoke.txt"
    res = CliRunner().invoke(cli.app, ["smoke", "--venue", "manifold", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "manifold" in out.read_text(encoding="utf-8")
