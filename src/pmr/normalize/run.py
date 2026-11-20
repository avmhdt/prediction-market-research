"""``pmr normalize``: raw archive -> interim Parquet, a pure function of the raw parts (FR-08).

Every raw part maps to at most one interim file per dataset, named after the part. A manifest
(``interim/_manifest.json``) records which parts are done, so a re-run only processes new
parts; ``--rebuild`` deletes ``interim/`` and processes everything again.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pmr.collectors.raw import iter_parts, part_id, read_part
from pmr.normalize import kalshi, manifold, metaculus, polymarket, polymarket_onchain
from pmr.normalize.categories import CategoryMap
from pmr.normalize.schema import frame
from pmr.normalize.store import interim_path, interim_root, write_interim

Rows = dict[str, list[dict[str, Any]]]
Handler = Callable[[dict[str, Any], CategoryMap], Rows]


def _fred(rec: dict[str, Any], _: CategoryMap) -> Rows:
    body = rec.get("body")
    text = body.get("text", "") if isinstance(body, dict) else ""
    rows: list[dict[str, Any]] = []
    for r in csv.DictReader(io.StringIO(str(text))):
        val = (r.get("DTB3") or "").strip()
        d = (r.get("observation_date") or r.get("DATE") or "").strip()
        if not d or not val or val == ".":
            continue
        rows.append(
            {
                "date": date.fromisoformat(d),
                "series": "DTB3",
                "rate_pct": float(val),
                "ingested_at": int(rec["ts_recv_ns"]),
            }
        )
    return {"risk_free": rows}


def _book(fn: Callable[[dict[str, Any], int], list[dict[str, Any]]]) -> Handler:
    def h(rec: dict[str, Any], _: CategoryMap) -> Rows:
        return {"book_l2": fn(rec, 0)}  # snapshot counter assigned in the warehouse

    return h


def _manifold_bets(rec: dict[str, Any], _: CategoryMap) -> Rows:
    t, p = manifold.bets(rec)
    return {"trades": t, "price_history": p}


def _metaculus_posts(rec: dict[str, Any], cats: CategoryMap) -> Rows:
    m, p = metaculus.posts(rec, cats)
    return {"markets": m, "price_history": p}


def _onchain(rec: dict[str, Any], _: CategoryMap) -> Rows:
    return polymarket_onchain.decode(rec)


HANDLERS: dict[tuple[str, str], Handler] = {
    ("kalshi", "markets"): lambda r, c: {"markets": kalshi.markets(r, c)},
    ("kalshi", "historical_markets"): lambda r, c: {"markets": kalshi.markets(r, c)},
    ("kalshi", "events"): lambda r, c: {"events": kalshi.events(r)},
    ("kalshi", "series"): lambda r, c: {"events": kalshi.series(r)},
    ("kalshi", "trades"): lambda r, c: {"trades": kalshi.trades(r)},
    ("kalshi", "candles"): lambda r, c: {"price_history": kalshi.candles(r)},
    ("kalshi", "books"): _book(kalshi.book),
    ("polymarket", "gamma_markets"): lambda r, c: {"markets": polymarket.markets(r, c)},
    ("polymarket", "data_trades"): lambda r, c: {"trades": polymarket.trades(r)},
    ("polymarket", "clob_prices"): lambda r, c: {"price_history": polymarket.price_history(r)},
    ("polymarket", "clob_books"): _book(polymarket.book),
    ("manifold", "markets"): lambda r, c: {"markets": manifold.markets(r, c)},
    ("manifold", "market_details"): lambda r, c: {"markets": manifold.markets(r, c)},
    ("manifold", "bets"): _manifold_bets,
    ("metaculus", "posts"): _metaculus_posts,
    ("fred", "dtb3"): _fred,
}
"""Raw (venue, endpoint) -> normalizer. Endpoints not listed (cutoff, on-chain) are skipped."""


@dataclass
class NormalizeStats:
    """Counts for one normalize run."""

    parts: int = 0
    skipped: int = 0
    rows: dict[str, int] = field(default_factory=dict)


def normalize_part(path: Path, cats: CategoryMap) -> Rows:
    """All dataset rows from one raw part (empty when the endpoint has no normalizer)."""
    venue, endpoint = path.parts[-4], path.parts[-3]
    handler = HANDLERS.get((venue, endpoint))
    if handler is None and venue == "polymarket" and endpoint.startswith("onchain_"):
        handler = _onchain
    out: Rows = {}
    if handler is None:
        return out
    for rec in read_part(path):
        if int(rec.get("status", 200)) >= 400:
            continue
        for ds, rows in handler(rec, cats).items():
            out.setdefault(ds, []).extend(rows)
    return out


def run(
    data_dir: Path,
    cats: CategoryMap,
    *,
    venue: str | None = None,
    rebuild: bool = False,
) -> NormalizeStats:
    """Normalize every raw part not yet in the manifest (all parts with ``rebuild``)."""
    root = interim_root(data_dir)
    manifest_path = root / "_manifest.json"
    if rebuild:
        if venue is None and root.exists():
            shutil.rmtree(root)
        elif venue is not None:
            for ds_dir in root.glob(f"*/venue={venue}"):
                shutil.rmtree(ds_dir)
    manifest: dict[str, list[str]] = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    if rebuild:
        manifest = {
            k: v for k, v in manifest.items() if venue is not None and not k.startswith(f"{venue}/")
        }
    stats = NormalizeStats()
    for path in iter_parts(data_dir, venue):
        pid = part_id(path)
        if pid in manifest:
            stats.skipped += 1
            continue
        rows = normalize_part(path, cats)
        p_venue, day = path.parts[-4], path.parts[-2].removeprefix("date=")
        stem = path.name.removesuffix(".jsonl.zst")
        written: list[str] = []
        for ds, rs in sorted(rows.items()):
            if not rs:
                continue
            df = frame(ds, rs)
            write_interim(df, interim_path(data_dir, ds, p_venue, day, f"{path.parts[-3]}-{stem}"))
            stats.rows[ds] = stats.rows.get(ds, 0) + df.height
            written.append(ds)
        manifest[pid] = written
        stats.parts += 1
        root.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path.with_name("_manifest.json.tmp")
        tmp.write_text(json.dumps(manifest, sort_keys=True, indent=0), encoding="utf-8")
        tmp.replace(manifest_path)
    return stats
