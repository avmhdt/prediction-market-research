"""On-chain Polymarket collection against a synthetic JSON-RPC gateway."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pmr.collectors.base import CollectContext, CollectorError, IngestStats
from pmr.collectors.polymarket_onchain import OnchainCollector, OnchainConfig, pad_address
from pmr.collectors.raw import iter_parts, read_part
from pmr.http.client import HttpClient, HttpError
from pmr.http.guard import GuardError, check_jsonrpc
from pmr.http.ratelimit import RateLimiterRegistry
from pmr.settings import PmrConfig, Settings
from tests.conftest import REPO

RPC = "https://gateway.tenderly.co/public/polygon"
BASE_T = 1_700_000_000
EX = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"


def oc(**kw: Any) -> OnchainConfig:
    raw: dict[str, Any] = {
        "rpc_url": RPC,
        "rate_limit_rps": 10_000,
        "max_block_range": 100,
        "market_start_block": 0,
        "fills_start_block": 0,
        "full_fills_until": "2023-11-14T22:23:20Z",  # BASE_T + 2 * 300 s -> block 300
        "sample": {"every_blocks": 50, "window_blocks": 10},
        "raw_budget_gb": 1.0,
        "exchanges": [
            {"name": "ex", "address": EX, "from_block": 0, "topic": "0xaa", "layout": "v1"}
        ],
        "oracles": ["0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49"],
        "market_events": [
            {
                "name": "prep",
                "addresses": ["0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"],
                "topics": ["0xab"],
                "filter_oracles_at": 2,
            },
            {
                "name": "uma",
                "addresses": ["0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49"],
                "topics": ["0xee", "0x56"],
            },
        ],
    }
    raw.update(kw)
    return OnchainConfig(raw)


class Gateway:
    """Head at block 400, 2 s blocks, one log per block, a cap of 60 results per query."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        m, p = body["method"], body["params"]
        if m == "eth_blockNumber":
            return httpx.Response(200, json={"id": 1, "result": hex(400)})
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            return httpx.Response(200, json={"id": 1, "result": {"timestamp": hex(BASE_T + 2 * n)}})
        flt = p[0]
        a, b = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        if b - a + 1 > 60:
            hint = f"[{hex(a)}, {hex(a + 59)}]"
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "error": {
                        "code": -32602,
                        "message": f"Query returned more than 20000 results. Try with "
                        f"this block range {hint}.",
                    },
                },
            )
        logs = [
            {"blockNumber": hex(n), "topics": flt["topics"][:1], "data": "0x"}
            for n in range(a, b + 1)
        ]
        return httpx.Response(200, json={"id": 1, "result": logs})


async def _ctx(tmp_path: Path, settings: Settings, cfg: PmrConfig) -> CollectContext:
    async def nosleep(_: float) -> None:
        return None

    http = HttpClient(
        settings, cfg.http, limits=RateLimiterRegistry(default_rps=10_000), sleep=nosleep
    )
    return CollectContext(
        http=http,
        settings=settings,
        cfg=cfg,
        data_dir=tmp_path,
        since=date(2023, 1, 1),
        until=None,
        flush_every=5,
    )


def _blocks(tmp_path: Path, endpoint: str) -> list[int]:
    out: list[int] = []
    for part in iter_parts(tmp_path, "polymarket", endpoint):
        for rec in read_part(part):
            out += [int(x["blockNumber"], 16) for x in rec["body"]["result"]]
    return sorted(out)


@respx.mock
async def test_market_layer_full_coverage_with_cap_splitting(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    gw = Gateway()
    respx.post(RPC).mock(side_effect=gw)
    ctx = await _ctx(tmp_path, settings, cfg)
    st = IngestStats()
    await OnchainCollector(ctx, oc()).markets(st)
    await ctx.http.aclose()
    assert _blocks(tmp_path, "onchain_prep") == list(range(0, 401))  # no gaps, no duplicates
    prep = next(c for c in gw.calls if c["method"] == "eth_getLogs")["params"][0]
    assert prep["topics"][2] == [pad_address("0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49")]
    assert any(
        c["method"] == "eth_getLogs" and c["params"][0]["topics"][0] == ["0xee", "0x56"]
        for c in gw.calls
    )
    # Resumable: a second run fetches nothing new.
    n = len(gw.calls)
    ctx = await _ctx(tmp_path, settings, cfg)
    await OnchainCollector(ctx, oc()).markets(IngestStats())
    await ctx.http.aclose()
    assert len(gw.calls) - n == 1  # only eth_blockNumber


@respx.mock
async def test_fills_full_then_sampled_windows(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    gw = Gateway()
    respx.post(RPC).mock(side_effect=gw)
    ctx = await _ctx(tmp_path, settings, cfg)
    await OnchainCollector(ctx, oc()).fills(IngestStats())
    await ctx.http.aclose()
    full = _blocks(tmp_path, "onchain_fills_full_ex")
    assert full == list(range(0, 300))
    sampled = _blocks(tmp_path, "onchain_fills_sample_ex")
    assert sampled[:10] == list(range(300, 310)) and 350 in sampled and 310 not in sampled
    assert all((b - 300) % 50 < 10 for b in sampled)
    flt = next(c for c in gw.calls if c["method"] == "eth_getLogs")["params"][0]
    assert flt["topics"][3] == pad_address(EX)  # taker rows only


@respx.mock
async def test_budget_stop_and_rate_limit_retry(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    gw = Gateway()
    first = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        first["n"] += 1
        if first["n"] == 1:
            return httpx.Response(429)
        if first["n"] == 2:
            return httpx.Response(
                200, json={"id": 1, "error": {"code": -32005, "message": "rate limited"}}
            )
        return gw(request)

    respx.post(RPC).mock(side_effect=flaky)
    ctx = await _ctx(tmp_path, settings, cfg)
    assert await OnchainCollector(ctx, oc()).head() == 400
    with pytest.raises(CollectorError, match="budget"):
        await OnchainCollector(ctx, oc(raw_budget_gb=0.0)).markets(IngestStats())
    await ctx.http.aclose()


def test_jsonrpc_guard() -> None:
    ok = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{}]}
    check_jsonrpc(RPC, ok)
    check_jsonrpc(RPC, {"method": "eth_call", "params": [{"to": "0x1", "data": "0x"}, "latest"]})
    for bad in (
        {"method": "eth_sendRawTransaction", "params": ["0x00"]},
        {"method": "eth_sendTransaction", "params": [{}]},
        {"method": "eth_sign", "params": []},
        {"method": "eth_call", "params": [{"to": "0x1", "data": "0x", "from": "0x2"}]},
        [ok],
    ):
        with pytest.raises(GuardError):
            check_jsonrpc(RPC, bad)
    with pytest.raises(GuardError, match="host"):
        check_jsonrpc("https://clob.polymarket.com/rpc", ok)
    with pytest.raises(GuardError, match="host"):
        check_jsonrpc("http://gateway.tenderly.co/public/polygon", ok)


def test_shipped_onchain_config_is_consistent() -> None:
    c = OnchainConfig.load(REPO / "config" / "onchain.yaml")
    assert c["rpc_url"].startswith("https://gateway.tenderly.co/")
    assert {e["layout"] for e in c["exchanges"]} == {"v1", "v2", "v3"}
    for e in c["market_events"]:
        assert all(t.startswith("0x") and len(t) == 66 for t in e["topics"])
    assert c["sample"] == {"every_blocks": 1800, "window_blocks": 300}


@respx.mock
async def test_patient_pause_on_sustained_rate_limits(
    tmp_path: Path, settings: Settings, cfg: PmrConfig
) -> None:
    gw = Gateway()
    calls = {"n": 0}

    def refuse_then_serve(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 9:  # longer than the client's 6 retries
            return httpx.Response(429)
        return gw(request)

    respx.post(RPC).mock(side_effect=refuse_then_serve)
    ctx = await _ctx(tmp_path, settings, cfg)
    col = OnchainCollector(ctx, oc())
    pauses: list[float] = []

    async def fake_sleep(s: float) -> None:
        pauses.append(s)

    col._sleep = fake_sleep
    assert await col.head() == 400
    assert pauses == [60.0]
    col2 = OnchainCollector(ctx, oc(patience_hours=0))
    col2._sleep = fake_sleep
    calls["n"] = 0
    with pytest.raises(HttpError):
        await col2.head()
    respx.post(RPC).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(HttpError):
        await col.head()  # not transient: fails at once
    await ctx.http.aclose()
