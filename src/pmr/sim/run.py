"""``pmr mm sim``: replay the market maker on recorded books and trades (FR-22).

``--market VENUE:SYMBOL`` runs one market; ``--set paper`` runs every market the paper service
recorded (``<data>/paper/market_set.json`` history). Books and trades come from the warehouse;
a market that resolved in the window settles at its result. Output: one row per market with
the P&L decomposition (:mod:`pmr.sim.pnl`), in USD.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from pmr.sim.mm import MMParams, simulate_mm

NS = 10**9


def frame(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> pl.DataFrame:
    """Run SQL and return a Polars frame."""
    out = pl.from_arrow(con.execute(sql, params or []).to_arrow_table())
    assert isinstance(out, pl.DataFrame)
    return out


def one(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> Any:
    """Run SQL and return the first row (or None)."""
    return con.execute(sql, params or []).fetchone()


COLUMNS = (
    "venue",
    "symbol",
    "n_fills",
    "q_end",
    "spread_capture",
    "inventory_mtm",
    "settlement",
    "fees",
    "total",
    "markout_300",
    "markout_300_n",
    "markout_3600",
    "markout_3600_n",
)


def _ns(d: date | None, end: bool = False, default: int = 0) -> int:
    if d is None:
        return default
    return (
        int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() + (86400 if end else 0)) * NS
    )


def paper_symbols(data_dir: Path) -> list[tuple[str, str]]:
    """Every market that was ever in the paper set (current members plus history)."""
    path = data_dir / "paper" / "market_set.json"
    if not path.exists():
        return []
    d = json.loads(path.read_text(encoding="utf-8"))
    keys = set(d.get("members", {}))
    keys |= {h["key"] for h in d.get("history", []) if h.get("event") == "added"}
    return sorted(tuple(k.split(":", 1)) for k in keys)


def run(
    warehouse: Path,
    params: MMParams,
    markets: list[tuple[str, str]],
    *,
    since: date | None = None,
    until: date | None = None,
) -> pl.DataFrame:
    """Simulate each market; rows in :data:`COLUMNS` order."""
    lo, hi = _ns(since), _ns(until, end=True, default=2**62)
    con = duckdb.connect(str(warehouse), read_only=True)
    rows = []
    try:
        for venue, symbol in markets:
            books = frame(
                con,
                "SELECT venue, symbol, seq, ts_recv, side, price, qty FROM book_l2 "
                "WHERE venue = ? AND symbol = ? AND ts_recv BETWEEN ? AND ?",
                [venue, symbol, lo, hi],
            )
            if books.is_empty():
                continue
            trades = frame(
                con,
                "SELECT venue, symbol, trade_id, ts_exchange, price, qty, side FROM trades "
                "WHERE venue = ? AND symbol = ? AND ts_exchange BETWEEN ? AND ?",
                [venue, symbol, lo, hi],
            )
            res_row = one(
                con,
                "SELECT settlement_value, resolved_ts FROM markets_current "
                "WHERE venue = ? AND symbol = ?",
                [venue, symbol],
            )
            settle = None
            if res_row and res_row[0] is not None and res_row[1] is not None and res_row[1] <= hi:
                settle = float(res_row[0])
            r = simulate_mm(books, trades, params, venue=venue, symbol=symbol, settlement=settle)
            rows.append(
                (
                    venue,
                    symbol,
                    len(r.fills),
                    r.q_end,
                    r.spread_capture,
                    r.inventory_mtm,
                    r.settlement,
                    r.fees,
                    r.total,
                    r.markouts.get(300, 0.0),
                    r.markout_n.get(300, 0),
                    r.markouts.get(3600, 0.0),
                    r.markout_n.get(3600, 0),
                )
            )
    finally:
        con.close()
    return pl.DataFrame(rows, schema=list(COLUMNS), orient="row")
