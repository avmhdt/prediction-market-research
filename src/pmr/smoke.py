"""``pmr smoke``: one read per venue endpoint family (SPEC section 7e).

Each probe prints status, latency and one record id. The command exits non-zero if a
required probe fails. Optional probes (keys not configured, or endpoints that may need
auth) are reported but never fail the run.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pmr.http.client import FetchResult, HttpClient, HttpError, rate_limits_from_config
from pmr.http.guard import GuardError
from pmr.settings import PmrConfig, Settings

FRED_DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass
class ProbeResult:
    """Outcome of one smoke probe."""

    source: str
    endpoint: str
    required: bool
    ok: bool
    status: str
    latency_ms: float | None
    record_id: str
    note: str = ""
    clock_offset_s: float | None = None


@dataclass
class SmokeContext:
    """State shared between probes (ids discovered by one probe and used by the next)."""

    http: HttpClient
    cfg: PmrConfig
    settings: Settings
    kalshi_ticker: str | None = None
    polymarket_token: str | None = None


ProbeFn = Callable[[SmokeContext], Awaitable[tuple[FetchResult, str, str]]]


@dataclass(frozen=True)
class Probe:
    """A named smoke check: ``fn`` returns (response, record id, note)."""

    source: str
    endpoint: str
    required: bool
    fn: ProbeFn
    needs: Callable[[SmokeContext], str | None] = lambda ctx: None


def _first(data: Any, *keys: str) -> Any:
    """Return the first list element of ``data`` (or ``data[key]`` for the first key present)."""
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v[0] if v else None
    return None


def _as_str(v: Any) -> str:
    return "" if v is None else str(v)


# ---------------------------------------------------------------------------------- Kalshi


async def _kalshi_markets(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.kalshi.base_url.rstrip("/")
    r = await ctx.http.get(
        f"{base}/markets", {"limit": 1, "status": "open", "mve_filter": "exclude"}
    )
    m = _first(r.data, "markets") or {}
    ctx.kalshi_ticker = _as_str(m.get("ticker")) or None
    return r, _as_str(m.get("ticker")), ""


async def _kalshi_orderbook(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.kalshi.base_url.rstrip("/")
    r = await ctx.http.get(f"{base}/markets/{ctx.kalshi_ticker}/orderbook")
    book = r.data.get("orderbook", {}) if isinstance(r.data, dict) else {}
    levels = sum(len(v or []) for v in book.values()) if isinstance(book, dict) else 0
    return r, _as_str(ctx.kalshi_ticker), f"{levels} price levels"


async def _kalshi_trades(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.kalshi.base_url.rstrip("/")
    r = await ctx.http.get(f"{base}/markets/trades", {"limit": 1})
    t = _first(r.data, "trades") or {}
    return r, _as_str(t.get("trade_id")), ""


async def _kalshi_historical(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.kalshi.base_url.rstrip("/")
    r = await ctx.http.get(f"{base}/historical/cutoff")
    note = ""
    if isinstance(r.data, dict):
        note = ", ".join(f"{k}={v}" for k, v in sorted(r.data.items()) if not isinstance(v, dict))
    return r, "cutoff", note[:120]


# ------------------------------------------------------------------------------ Polymarket


async def _pm_gamma(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.polymarket.gamma_url.rstrip("/")
    r = await ctx.http.get(
        f"{base}/markets",
        {
            "limit": 5,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        },
    )
    markets = r.data if isinstance(r.data, list) else []
    cid = ""
    for m in markets:
        if not isinstance(m, dict):
            continue
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except ValueError:
                tokens = None
        if isinstance(tokens, list) and tokens:
            ctx.polymarket_token = str(tokens[0])
            cid = _as_str(m.get("conditionId"))
            break
    return r, cid, f"{len(markets)} markets"


async def _pm_clob_book(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.polymarket.clob_url.rstrip("/")
    r = await ctx.http.get(f"{base}/book", {"token_id": _as_str(ctx.polymarket_token)})
    d = r.data if isinstance(r.data, dict) else {}
    n = len(d.get("bids") or []) + len(d.get("asks") or [])
    tok = _as_str(ctx.polymarket_token)
    return r, (tok[:12] + "...") if len(tok) > 15 else tok, f"{n} price levels"


async def _pm_data_trades(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.polymarket.data_url.rstrip("/")
    r = await ctx.http.get(f"{base}/trades", {"limit": 1})
    t = _first(r.data, "data") or {}
    tx = _as_str(t.get("transaction_hash") or t.get("transactionHash"))
    return r, (tx[:14] + "...") if len(tx) > 17 else tx, ""


async def _pm_geoblock(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    r = await ctx.http.get(POLYMARKET_GEOBLOCK_URL)
    d = r.data if isinstance(r.data, dict) else {}
    # Only the verdict and the country are kept; the response's IP address is dropped.
    note = f"blocked={d.get('blocked')} country={d.get('country')}"
    return r, "geoblock", note


# ---------------------------------------------------------------------- Manifold, Metaculus


async def _manifold(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.manifold.base_url.rstrip("/")
    r = await ctx.http.get(f"{base}/markets", {"limit": 1})
    m = _first(r.data) or {}
    return r, _as_str(m.get("id")), ""


async def _metaculus(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    base = ctx.cfg.venues.metaculus.base_url.rstrip("/")
    token = ctx.settings.metaculus_api_token
    headers = {"Authorization": f"Token {token.get_secret_value()}"} if token else {}
    r = await ctx.http.get(f"{base}/posts/", {"limit": 1}, headers=headers)
    q = _first(r.data, "results") or {}
    return r, _as_str(q.get("id")), ""


# -------------------------------------------------------------------------------- FRED


async def _fred(ctx: SmokeContext) -> tuple[FetchResult, str, str]:
    r = await ctx.http.get(FRED_DTB3_URL, expect_json=False)
    text = r.data.get("text", "") if isinstance(r.data, dict) else ""
    rows = list(csv.reader(io.StringIO(str(text))))
    last = rows[-1] if len(rows) > 1 else []
    return r, _as_str(last[0] if last else ""), f"header={','.join(rows[0]) if rows else ''}"


def default_probes() -> list[Probe]:
    """The probes run by ``pmr smoke``, in order."""
    no_metaculus_token: Callable[[SmokeContext], str | None] = lambda ctx: (  # noqa: E731
        None if ctx.settings.metaculus_api_token else "METACULUS_API_TOKEN not set (P-06)"
    )
    return [
        Probe("kalshi", "GET /markets", True, _kalshi_markets),
        Probe(
            "kalshi",
            "GET /markets/{ticker}/orderbook",
            True,
            _kalshi_orderbook,
            lambda c: None if c.kalshi_ticker else "no ticker from /markets",
        ),
        Probe("kalshi", "GET /markets/trades", True, _kalshi_trades),
        Probe("kalshi", "GET /historical/cutoff", True, _kalshi_historical),
        Probe("polymarket-gamma", "GET /markets", True, _pm_gamma),
        Probe(
            "polymarket-clob",
            "GET /book",
            True,
            _pm_clob_book,
            lambda c: None if c.polymarket_token else "no token id from Gamma",
        ),
        Probe("polymarket-data", "GET /trades", True, _pm_data_trades),
        Probe("polymarket", "GET /api/geoblock", False, _pm_geoblock),
        Probe("manifold", "GET /markets", True, _manifold),
        Probe("metaculus", "GET /posts/", False, _metaculus, no_metaculus_token),
        Probe("fred", "GET fredgraph.csv?id=DTB3", True, _fred),
    ]


async def run_probes(
    settings: Settings,
    cfg: PmrConfig,
    *,
    probes: Sequence[Probe] | None = None,
    sources: Sequence[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ProbeResult]:
    """Run the probes sequentially and return one result per probe."""
    results: list[ProbeResult] = []
    async with HttpClient(
        settings,
        cfg.http,
        limits=rate_limits_from_config(cfg),
        transport=transport,
        seed=cfg.seed,
    ) as http:
        ctx = SmokeContext(http=http, cfg=cfg, settings=settings)
        for p in probes if probes is not None else default_probes():
            if sources and not any(p.source.startswith(s) for s in sources):
                continue
            skip = p.needs(ctx)
            if skip:
                results.append(
                    ProbeResult(
                        p.source, p.endpoint, p.required, not p.required, "skipped", None, "", skip
                    )
                )
                continue
            t0 = time.perf_counter()
            try:
                resp, rid, note = await p.fn(ctx)
                results.append(
                    ProbeResult(
                        p.source,
                        p.endpoint,
                        p.required,
                        True,
                        str(resp.status),
                        round(resp.elapsed_s * 1000, 1),
                        rid,
                        note,
                        resp.clock_offset_s,
                    )
                )
            except (HttpError, GuardError, ValueError, KeyError, TypeError) as exc:
                status = str(exc.status) if isinstance(exc, HttpError) and exc.status else "error"
                results.append(
                    ProbeResult(
                        p.source,
                        p.endpoint,
                        p.required,
                        False,
                        status,
                        round((time.perf_counter() - t0) * 1000, 1),
                        "",
                        f"{type(exc).__name__}: {exc}"[:160],
                    )
                )
    return results


def format_report(results: Sequence[ProbeResult], *, owner_country: str) -> str:
    """Plain-text report written to the console and ``reports/smoke.txt``."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"pmr smoke  {now}  OWNER_COUNTRY={owner_country}",
        "",
        f"{'source':<18} {'endpoint':<32} {'req':<4} {'status':<8} {'ms':>8}  "
        f"{'clock_s':>8}  record_id  note",
    ]
    for r in results:
        ms = f"{r.latency_ms:.0f}" if r.latency_ms is not None else "-"
        off = f"{r.clock_offset_s:+.2f}" if r.clock_offset_s is not None else "-"
        flag = "" if r.ok else ("  FAIL" if r.required else "  (optional, not ok)")
        lines.append(
            f"{r.source:<18} {r.endpoint:<32} {'yes' if r.required else 'no':<4} "
            f"{r.status:<8} {ms:>8}  {off:>8}  {r.record_id}  {r.note}{flag}"
        )
    failed = [r for r in results if r.required and not r.ok]
    lines += [
        "",
        "clock_s = local receipt time minus the server Date header (1 s resolution; "
        "DQ-01 flags offsets over 2 s).",
        f"required probes failed: {len(failed)}",
        "",
    ]
    return "\n".join(lines)


def exit_code(results: Sequence[ProbeResult]) -> int:
    """0 when every required probe passed, else 1."""
    return 1 if any(r.required and not r.ok for r in results) else 0


def run(
    settings: Settings, cfg: PmrConfig, sources: Sequence[str] | None = None
) -> list[ProbeResult]:
    """Synchronous wrapper used by the CLI."""
    return asyncio.run(run_probes(settings, cfg, sources=sources))
