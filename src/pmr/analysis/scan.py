"""``pmr arb scan``: executable edges on recorded books (FR-20).

* ``--historical --since --until``: every recorder snapshot in the window.
* ``--live``: the latest snapshot of each symbol.

Two scans run on the warehouse's ``book_l2``:

* intra-venue YES + NO on Polymarket (the YES token's asks against the NO token's own asks);
  a SQL pass first keeps only snapshots whose best YES ask plus best NO ask is below 1, so the
  depth walk (with fees, lockup and haircut, :func:`pmr.analysis.arb.walk`) runs only there;
* cross-venue pairs from ``config/contract_map.yaml`` (confirmed only), on snapshots of the two
  symbols taken within ``max_skew_s`` of each other.

Multi-outcome sets are monitored live by the paper service (their completeness needs the event's
outcome count, which the recorder knows at poll time).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import yaml

from pmr.analysis.arb import (
    ArbConfig,
    ArbOpportunity,
    ContractPair,
    LegInfo,
    buy_yes_levels,
    scan_pair,
    scan_yes_no,
    to_frame,
)

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


@dataclass(frozen=True)
class ScanResult:
    """Opportunities found and how many snapshots were looked at."""

    rows: pl.DataFrame
    snapshots_checked: int


def _ns(d: date, end: bool = False) -> int:
    return (
        int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() + (86400 if end else 0)) * NS
    )


def _book(con: duckdb.DuckDBPyConnection, venue: str, symbol: str, seq: int) -> pl.DataFrame:
    return frame(
        con,
        "SELECT side, price, qty FROM book_l2 WHERE venue = ? AND symbol = ? AND seq = ?",
        [venue, symbol, seq],
    )


def load_pairs(path: Path) -> list[dict[str, Any]]:
    """Confirmed pairs from the manual map."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [p for p in data.get("pairs") or [] if p.get("status") == "confirmed"]


def scan(
    warehouse: Path,
    cfg: ArbConfig,
    *,
    since: date | None = None,
    until: date | None = None,
    live: bool = False,
    pairs_path: Path = Path("config/contract_map.yaml"),
    max_skew_s: float = 60.0,
) -> ScanResult:
    """Run the intra-venue and cross-venue scans on recorded snapshots."""
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        lo = _ns(since) if since else 0
        hi = _ns(until, end=True) if until else 2**62
        latest = "WHERE rn_latest = 1" if live else ""
        candidates = frame(
            con,
            f"""
            WITH m AS (
                SELECT symbol, no_symbol, close_ts, category FROM markets_current
                WHERE venue = 'polymarket' AND no_symbol IS NOT NULL
            ), y AS (
                SELECT b.symbol, b.seq, b.ts, b.ask_px, m.no_symbol, m.close_ts, m.category
                FROM bbo b JOIN m ON b.symbol = m.symbol
                WHERE b.venue = 'polymarket' AND b.ts BETWEEN {lo} AND {hi}
            ), paired AS (
                -- each YES snapshot with the NO-token snapshot nearest in time
                SELECT y.symbol, y.seq AS seq_yes, n.seq AS seq_no, y.ts, y.close_ts,
                       y.category, y.no_symbol, y.ask_px + n.ask_px AS cost
                FROM y JOIN bbo n
                  ON n.venue = 'polymarket' AND n.symbol = y.no_symbol
                 AND abs(n.ts - y.ts) <= {int(max_skew_s * NS)}
                WHERE y.ask_px IS NOT NULL AND n.ask_px IS NOT NULL
                QUALIFY row_number() OVER (
                    PARTITION BY y.symbol, y.seq ORDER BY abs(n.ts - y.ts), n.seq
                ) = 1
            )
            SELECT * FROM (
                SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn_latest
                FROM paired
            ) {latest}
            """,
        )
        checked = candidates.height
        out: list[ArbOpportunity] = []
        for r in candidates.filter(pl.col("cost") < 1.0).iter_rows(named=True):
            leg = LegInfo(
                "polymarket", r["symbol"], int(r["close_ts"] or r["ts"]), category=r["category"]
            )
            yes = buy_yes_levels(_book(con, "polymarket", r["symbol"], r["seq_yes"]))
            no = buy_yes_levels(_book(con, "polymarket", r["no_symbol"], r["seq_no"]))
            out += scan_yes_no(leg, yes, no, int(r["ts"]), cfg)
        for p in load_pairs(pairs_path):
            a, b = dict(p["a"]), dict(p["b"])
            for side_leg in (a, b):  # the manual map names markets; books are keyed by symbol
                if not side_leg.get("symbol"):
                    row = one(
                        con,
                        "SELECT symbol FROM markets_current WHERE venue = ? AND market_id = ?",
                        [side_leg["venue"], str(side_leg["market_id"])],
                    )
                    side_leg["symbol"] = row[0] if row else str(side_leg["market_id"])
            snaps = frame(
                con,
                f"""
                SELECT x.seq AS sa, y.seq AS sb, x.ts, ma.close_ts AS ca, mb.close_ts AS cb
                FROM bbo x
                JOIN bbo y ON y.venue = ? AND y.symbol = ?
                  AND abs(y.ts - x.ts) <= {int(max_skew_s * NS)}
                JOIN markets_current ma ON ma.venue = x.venue AND ma.symbol = x.symbol
                JOIN markets_current mb ON mb.venue = y.venue AND mb.symbol = y.symbol
                WHERE x.venue = ? AND x.symbol = ? AND x.ts BETWEEN {lo} AND {hi}
                """,
                [b["venue"], b["symbol"], a["venue"], a["symbol"]],
            )
            checked += snaps.height
            pair = ContractPair(
                pair_id=str(
                    p.get("pair_id") or f"{a['venue']}:{a['symbol']}|{b['venue']}:{b['symbol']}"
                ),
                a=LegInfo(a["venue"], a["symbol"], 0),
                b=LegInfo(b["venue"], b["symbol"], 0),
                orientation=int(p.get("orientation", 1)),
                material_diffs=tuple(p.get("material_diffs") or ()),
            )
            for s in snaps.iter_rows(named=True):
                pr = ContractPair(
                    pair.pair_id,
                    LegInfo(a["venue"], a["symbol"], int(s["ca"] or s["ts"])),
                    LegInfo(b["venue"], b["symbol"], int(s["cb"] or s["ts"])),
                    pair.orientation,
                    pair.material_diffs,
                )
                out += scan_pair(
                    _book(con, a["venue"], a["symbol"], s["sa"]),
                    _book(con, b["venue"], b["symbol"], s["sb"]),
                    pr,
                    int(s["ts"]),
                    cfg,
                )
        return ScanResult(to_frame(out) if out else pl.DataFrame(), checked)
    finally:
        con.close()
