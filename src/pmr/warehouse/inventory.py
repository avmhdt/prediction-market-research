"""``pmr inventory``: ``reports/data_inventory.md`` (FR-11, DQ-05, DQ-07, DQ-09, DQ-15, NFR-13).

Counts only (aggregates). Markets are the current versions whose close time lies in the study
window. "Eligible" means a final YES/NO result (DQ-05), not play money and not a forecast,
and at least ``min_trades`` trades and ``min_notional`` USD notional at or before the close
(DQ-07; the horizon-specific filter is applied again at each calibration snapshot in M5).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

REAL_MONEY = ("kalshi", "polymarket")
MIN_TOTAL = 3000
MIN_PER_VENUE = 1000
MAX_OTHER_SHARE = 0.15


@dataclass(frozen=True)
class InventoryResult:
    """Headline numbers plus the Markdown text."""

    eligible_by_venue: dict[str, int]
    eligible_total: int
    other_share: float | None
    markdown: str

    @property
    def meets_m2_exit(self) -> bool:
        """At least 3,000 eligible real-money markets in total."""
        return self.eligible_total >= MIN_TOTAL


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def _table(header: list[str], rows: list[tuple[object, ...]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    if not rows:
        out.append("| " + " | ".join(["(none)"] + [""] * (len(header) - 1)) + " |")
    return out


def inventory(
    warehouse: Path,
    data_dir: Path,
    *,
    since: date,
    until: date | None,
    min_trades: int,
    min_notional: float,
    now: datetime | None = None,
) -> InventoryResult:
    """Compute the inventory and render it as Markdown."""
    lo = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp()) * 10**9
    hi = (
        int(datetime(until.year, until.month, until.day, tzinfo=UTC).timestamp() + 86400) * 10**9
        if until
        else 2**62
    )
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        con.execute(
            f"""
            CREATE TEMP VIEW w AS
            SELECT m.*, coalesce(l.n_trades, 0) AS n_trades_pre,
                   coalesce(l.notional, 0) AS notional_pre
            FROM markets_current m
            LEFT JOIN (
                SELECT t.venue, t.market_id, count(*) AS n_trades, sum(t.notional) AS notional
                FROM trades t JOIN markets_current mc
                  ON t.venue = mc.venue AND t.market_id = mc.market_id
                WHERE t.ts_exchange <= mc.close_ts
                GROUP BY t.venue, t.market_id
            ) l ON m.venue = l.venue AND m.market_id = l.market_id
            WHERE m.close_ts >= {lo} AND m.close_ts < {hi}
            """
        )
        q = con.execute
        by_status = q(
            "SELECT venue, status, count(*) FROM w GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        by_year = q(
            "SELECT venue, year(make_timestamp(close_ts // 1000)) AS y, count(*) "
            "FROM w GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        resolved = "status = 'resolved' AND result IN ('YES', 'NO')"
        by_cat = q(
            f"SELECT venue, category, count(*) FROM w WHERE {resolved} "
            "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
        ).fetchall()
        excl = q(
            f"""
            SELECT venue,
              count(*) FILTER (WHERE result = 'VOID' OR status = 'voided') AS voided,
              count(*) FILTER (WHERE result = 'SCALAR') AS scalar,
              count(*) FILTER (WHERE status = 'disputed') AS disputed_unresolved,
              count(*) FILTER (WHERE status IN ('open', 'closed') AND result IS NULL)
                AS unresolved,
              count(*) FILTER (WHERE {resolved} AND disputed) AS resolved_after_dispute,
              count(*) FILTER (WHERE {resolved} AND n_trades_pre < {int(min_trades)})
                AS lt_min_trades,
              count(*) FILTER (WHERE {resolved} AND n_trades_pre >= {int(min_trades)}
                AND notional_pre < {float(min_notional)}) AS lt_min_notional
            FROM w GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        elig_rows = q(
            f"""
            SELECT venue, count(*) FROM w
            WHERE {resolved} AND NOT is_play_money AND NOT is_forecast
              AND n_trades_pre >= {int(min_trades)} AND notional_pre >= {float(min_notional)}
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        other = q(
            f"""
            SELECT count(*) FILTER (WHERE category = 'other'), count(*)
            FROM w WHERE {resolved} AND NOT is_play_money AND NOT is_forecast
            """
        ).fetchone()
        gaps = q(
            f"""
            SELECT w.venue,
              count(*) FILTER (WHERE w.n_trades_pre = 0) AS no_trades,
              count(*) FILTER (WHERE ph.symbol IS NULL) AS no_price_history
            FROM w LEFT JOIN (SELECT DISTINCT venue, symbol FROM price_history) ph
              ON w.venue = ph.venue AND w.symbol = ph.symbol
            WHERE {resolved}
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        table_rows = {
            t: int(q(f"SELECT count(*) FROM {t}").fetchone()[0])  # type: ignore[index]
            for t in ("markets", "trades", "price_history", "book_l2", "risk_free")
        }
    finally:
        con.close()

    eligible = {str(v): int(n) for v, n in elig_rows}
    total = sum(n for v, n in eligible.items() if v in REAL_MONEY)
    other_share = (other[0] / other[1]) if other and other[1] else None
    raw_bytes = {p.name: _dir_bytes(p) for p in sorted((data_dir / "raw").glob("*")) if p.is_dir()}
    disk = shutil.disk_usage(data_dir if data_dir.exists() else Path.cwd())
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    window = f"{since.isoformat()} to {until.isoformat() if until else 'latest'}"

    md: list[str] = [
        "# Data inventory",
        "",
        f"Generated {stamp} by `pmr inventory` from the warehouse. Study window (D-01): "
        f"markets whose close time falls in {window}. Counts only.",
        "",
        "## Eligible resolved real-money markets",
        "",
        f"Final YES/NO result (DQ-05), real money, at least {min_trades} trades and "
        f"USD {min_notional:,.0f} notional at or before the close (DQ-07). M2 needs "
        f"{MIN_TOTAL:,} in total; AC-W-01 later needs {MIN_PER_VENUE:,} per real-money venue.",
        "",
        *_table(
            ["venue", "eligible", f"≥ {MIN_PER_VENUE:,}?"],
            [
                (
                    v,
                    f"{eligible.get(v, 0):,}",
                    "yes" if eligible.get(v, 0) >= MIN_PER_VENUE else "no",
                )
                for v in REAL_MONEY
            ],
        ),
        "",
        f"Total eligible real-money markets: **{total:,}** "
        f"({'meets' if total >= MIN_TOTAL else 'below'} the M2 threshold of {MIN_TOTAL:,}).",
        "",
        "Play-money and forecast venues (never pooled with real money, DQ-10): "
        + (", ".join(f"{v} {n:,}" for v, n in eligible.items() if v not in REAL_MONEY) or "none")
        + ".",
        "",
        "## Markets by venue and status (DQ-09: no survivorship filter)",
        "",
        *_table(["venue", "status", "markets"], [(v, s, f"{n:,}") for v, s, n in by_status]),
        "",
        "## Markets by venue and close year",
        "",
        *_table(["venue", "close year", "markets"], [(v, y, f"{n:,}") for v, y, n in by_year]),
        "",
        "## Resolved YES/NO markets by venue and category (D-08)",
        "",
        *_table(["venue", "category", "markets"], [(v, c, f"{n:,}") for v, c, n in by_cat]),
        "",
        "Share of `other` among real-money resolved markets (DQ-15, must be below 15%): "
        + (f"**{other_share:.1%}**" if other_share is not None else "n/a")
        + ".",
        "",
        "## Excluded markets by reason",
        "",
        *_table(
            [
                "venue",
                "voided",
                "scalar / 50-50",
                "disputed, unresolved",
                "no final result",
                "resolved after a dispute (flagged, kept)",
                f"< {min_trades} trades",
                f"< USD {min_notional:,.0f} notional",
            ],
            [tuple(f"{x:,}" if isinstance(x, int) else x for x in r) for r in excl],
        ),
        "",
        "## Coverage gaps (resolved YES/NO markets)",
        "",
        *_table(
            ["venue", "no trades before close", "no price history"],
            [(v, f"{a:,}", f"{b:,}") for v, a, b in gaps],
        ),
        "",
        "## Disk use (NFR-13: raw ≤ 20 GB, warehouse ≤ 30 GB)",
        "",
        *_table(
            ["item", "size"],
            [
                *[(f"raw/{k}", _gb(v)) for k, v in raw_bytes.items()],
                ("raw total", _gb(sum(raw_bytes.values()))),
                ("interim", _gb(_dir_bytes(data_dir / "interim"))),
                ("warehouse", _gb(warehouse.stat().st_size if warehouse.exists() else 0)),
                ("free on the data filesystem", _gb(disk.free)),
            ],
        ),
        "",
        "## Warehouse rows",
        "",
        *_table(["table", "rows"], [(k, f"{v:,}") for k, v in table_rows.items()]),
        "",
    ]
    return InventoryResult(eligible, total, other_share, "\n".join(md))
