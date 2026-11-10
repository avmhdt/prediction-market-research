"""DuckDB warehouse: build from interim Parquet, migrate, stats and checksums (FR-10, AC-W-04).

``build`` always starts from an empty file (written to ``*.tmp`` and renamed), applies
``ddl.sql`` and every ``migrations/NNN_*.sql``, loads the interim datasets and creates the
views. The result depends only on the interim files, which depend only on the raw archive, so
two builds give identical per-table checksums.

Loading rules:

* markets: Type-2 history (DQ-03). A lite record is dropped when a full record (with rules
  text) of the same market exists. Consecutive identical versions collapse; ``valid_to`` is
  the next version's ``valid_from``. ``rules_changed`` flags a version whose rules differ from
  the first version's. Kalshi markets get their series and category from events and series.
* trades: unique on ``(venue, trade_id)`` (DQ-08), keeping the live API record over the
  historical one over the on-chain one; Polymarket NO prints get the market's YES symbol.
* price_history and risk_free: the latest ingest wins for a duplicate key.
* book_l2: ``seq`` is the snapshot counter per symbol, in ``ts_recv`` order.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from pmr.normalize.categories import CategoryMap
from pmr.normalize.schema import MARKETS
from pmr.normalize.store import interim_files, read_interim

HERE = Path(__file__).resolve().parent
DDL = HERE / "ddl.sql"
VIEWS = HERE / "views.sql"
MIGRATIONS = HERE / "migrations"
TABLES = ("markets", "events", "trades", "price_history", "book_l2", "risk_free")
VERSION_COLS = [c for c in MARKETS if c not in ("valid_from", "ingested_at")]
SOURCE_RANK = "CASE source WHEN 'api' THEN 0 WHEN 'historical' THEN 1 ELSE 2 END"


@dataclass(frozen=True)
class Migration:
    """One numbered schema migration."""

    version: int
    path: Path


def migrations() -> list[Migration]:
    """``migrations/NNN_*.sql`` sorted by number (version 1 is ``ddl.sql``)."""
    out = []
    for p in sorted(MIGRATIONS.glob("*.sql")):
        m = re.match(r"(\d+)_", p.name)
        if m:
            out.append(Migration(int(m.group(1)), p))
    return out


def _connect(path: Path, threads: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(f"SET threads = {int(threads)}")
    con.execute("SET preserve_insertion_order = true")
    return con


def schema_version(con: duckdb.DuckDBPyConnection) -> int:
    """Highest applied schema version."""
    row = con.execute("SELECT max(version) FROM meta_schema").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def apply_migrations(con: duckdb.DuckDBPyConnection) -> list[int]:
    """Apply every migration newer than the current version; recreate views."""
    applied: list[int] = []
    current = schema_version(con)
    for mig in migrations():
        if mig.version > current:
            con.execute(mig.path.read_text(encoding="utf-8"))
            applied.append(mig.version)
    con.execute(VIEWS.read_text(encoding="utf-8"))
    return applied


def version_markets(raw: pl.DataFrame, events: pl.DataFrame, cats: CategoryMap) -> pl.DataFrame:
    """Collapse market snapshots into Type-2 versions with ``valid_to`` and ``rules_changed``."""
    if raw.is_empty():
        return raw.with_columns(
            pl.lit(None, pl.Int64).alias("valid_to"), pl.lit(False).alias("rules_changed")
        )
    m = raw
    # Kalshi: series and category from events (event -> series) and series (-> category).
    if events.height and m.filter(pl.col("venue") == "kalshi").height:
        ev = (
            events.filter(pl.col("event_id").is_not_null())
            .sort("ingested_at")
            .unique(subset=["venue", "event_id"], keep="last")
            .select(
                "venue",
                "event_id",
                pl.col("series_id").alias("_ev_series"),
                pl.col("category_raw").alias("_ev_cat"),
            )
        )
        se = (
            events.filter(pl.col("event_id").is_null())
            .sort("ingested_at")
            .unique(subset=["venue", "series_id"], keep="last")
            .select("venue", "series_id", pl.col("category_raw").alias("_se_cat"))
        )
        m = m.join(ev, on=["venue", "event_id"], how="left")
        m = m.with_columns(pl.coalesce("series_id", "_ev_series").alias("series_id"))
        m = m.join(se, on=["venue", "series_id"], how="left")
        m = m.with_columns(
            pl.coalesce("category_raw", "_se_cat", "_ev_cat").alias("category_raw")
        ).drop("_ev_series", "_ev_cat", "_se_cat")
    m = m.with_columns(
        pl.struct("venue", "category_raw", "category")
        .map_elements(
            lambda s: s["category"] or cats.map(s["venue"], (s["category_raw"] or "").split("|")),
            return_dtype=pl.String,
        )
        .alias("category")
    )
    # Drop lite records where a full record of the same market exists.
    has_full = m.group_by("venue", "symbol").agg(
        pl.col("rules_text").is_not_null().any().alias("_any_full")
    )
    m = (
        m.join(has_full, on=["venue", "symbol"], how="left")
        .filter(~pl.col("_any_full") | pl.col("rules_text").is_not_null())
        .drop("_any_full")
    )
    m = m.with_columns(
        pl.concat_str(
            [pl.col(c).cast(pl.String).fill_null("\x00") for c in VERSION_COLS], separator="\x1f"
        )
        .hash(seed=0)
        .alias("_h")
    )
    m = m.sort(["venue", "symbol", "valid_from", "_h"]).unique(
        subset=["venue", "symbol", "valid_from"], keep="first", maintain_order=True
    )
    m = m.with_columns(pl.col("_h").shift(1).over(["venue", "symbol"]).alias("_prev"))
    m = m.filter(pl.col("_prev").is_null() | (pl.col("_h") != pl.col("_prev")))
    m = m.with_columns(
        pl.col("valid_from").shift(-1).over(["venue", "symbol"]).alias("valid_to"),
        pl.col("rules_hash").first().over(["venue", "symbol"]).alias("_first_rules"),
    )
    m = m.with_columns(
        (
            pl.col("rules_hash").is_not_null()
            & pl.col("_first_rules").is_not_null()
            & (pl.col("rules_hash") != pl.col("_first_rules"))
        ).alias("rules_changed")
    ).drop("_h", "_prev", "_first_rules")
    return m.sort(["venue", "symbol", "valid_from"])


def _parquet_list(files: list[Path]) -> str:
    # Callers pass hive_partitioning = false: the interim path has a ``date=`` level (the raw
    # part's day) that must not shadow risk_free's own ``date`` column.
    return "[" + ", ".join("'" + str(f).replace("'", "''") + "'" for f in files) + "]"


@dataclass(frozen=True)
class BuildResult:
    """Row counts per table after a build."""

    path: Path
    rows: dict[str, int]


def build(data_dir: Path, path: Path, cats: CategoryMap, *, threads: int = 8) -> BuildResult:
    """Rebuild the warehouse at ``path`` from ``<data_dir>/interim``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    for p in (tmp, Path(str(tmp) + ".wal")):
        if p.exists():
            p.unlink()
    con = _connect(tmp, threads)
    try:
        con.execute(DDL.read_text(encoding="utf-8"))
        for mig in migrations():
            con.execute(mig.path.read_text(encoding="utf-8"))

        events = read_interim(data_dir, "events")
        markets = version_markets(read_interim(data_dir, "markets"), events, cats)
        cols = [*MARKETS.keys(), "rules_changed", "valid_to"]
        con.register("m_df", markets.select(cols).to_arrow())
        con.execute(f"INSERT INTO markets ({', '.join(cols)}) SELECT {', '.join(cols)} FROM m_df")
        con.unregister("m_df")
        if events.height:
            con.register(
                "e_df",
                events.unique()
                .sort(["venue", "event_id", "series_id", "ingested_at"], nulls_last=True)
                .to_arrow(),
            )
            con.execute("INSERT INTO events SELECT * FROM e_df")
            con.unregister("e_df")

        files = interim_files(data_dir, "trades")
        if files:
            con.execute(
                f"""
                INSERT INTO trades
                SELECT t.ts_exchange, t.ts_recv, t.venue, coalesce(t.symbol, m.symbol),
                       t.trade_id, t.price, t.qty, t.side, t.market_id, t.source, t.notional
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY venue, trade_id
                        ORDER BY {SOURCE_RANK}, ts_recv, ts_exchange, price, qty
                    ) AS rn
                    FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                    WHERE ts_exchange IS NOT NULL AND price BETWEEN 0 AND 1
                ) t
                LEFT JOIN (
                    SELECT venue, market_id, min(symbol) AS symbol
                    FROM markets WHERE valid_to IS NULL GROUP BY venue, market_id
                ) m ON t.venue = m.venue AND t.market_id = m.market_id
                WHERE t.rn = 1
                ORDER BY t.venue, t.market_id, t.ts_exchange, t.trade_id
                """
            )
        files = interim_files(data_dir, "price_history")
        if files:
            con.execute(
                f"""
                INSERT INTO price_history
                SELECT venue, symbol, ts, interval_s, open, high, low, close, yes_bid_close,
                       yes_ask_close, volume, open_interest, n_forecasters
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY venue, symbol, ts, interval_s
                        ORDER BY ingested_at DESC, close
                    ) AS rn
                    FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                    WHERE ts IS NOT NULL AND symbol IS NOT NULL
                )
                WHERE rn = 1
                ORDER BY venue, symbol, interval_s, ts
                """
            )
        files = interim_files(data_dir, "book_l2")
        if files:
            con.execute(
                f"""
                INSERT INTO book_l2
                SELECT ts_exchange, ts_recv, venue, symbol,
                       dense_rank() OVER (PARTITION BY venue, symbol ORDER BY ts_recv) AS seq,
                       side, price, qty, is_snapshot
                FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                WHERE symbol IS NOT NULL
                ORDER BY venue, symbol, ts_recv, side, price
                """
            )
        files = interim_files(data_dir, "risk_free")
        if files:
            con.execute(
                f"""
                INSERT INTO risk_free
                SELECT "date", series, rate_pct FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY series, "date" ORDER BY ingested_at DESC, rate_pct
                    ) AS rn
                    FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                ) WHERE rn = 1 ORDER BY series, "date"
                """
            )
        con.execute(VIEWS.read_text(encoding="utf-8"))
        rows = table_counts(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, path)
    return BuildResult(path=path, rows=rows)


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count per warehouse table."""
    out: dict[str, int] = {}
    for t in TABLES:
        row = con.execute(f"SELECT count(*) FROM {t}").fetchone()
        out[t] = int(row[0]) if row else 0
    return out


def checksums(path: Path) -> dict[str, str]:
    """Order-independent checksum per table: row count plus sum and xor of row hashes."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        out: dict[str, str] = {}
        for t in ("meta_schema", *TABLES):
            row = con.execute(
                f"SELECT count(*), coalesce(sum(hash(x)::HUGEINT), 0), "
                f"coalesce(bit_xor(hash(x)), 0) FROM {t} AS x"
            ).fetchone()
            assert row is not None
            out[t] = f"{row[0]}:{row[1]:x}:{row[2]:x}"
        return out
    finally:
        con.close()


def migrate(path: Path) -> list[int]:
    """Apply pending migrations to an existing warehouse file."""
    con = _connect(path, 2)
    try:
        return apply_migrations(con)
    finally:
        con.close()


def stats(path: Path) -> dict[str, dict[str, int]]:
    """Rows per table and venue."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        out: dict[str, dict[str, int]] = {}
        for t in TABLES:
            if t == "risk_free":
                rows = con.execute("SELECT series, count(*) FROM risk_free GROUP BY 1").fetchall()
            else:
                rows = con.execute(f"SELECT venue, count(*) FROM {t} GROUP BY 1").fetchall()
            out[t] = {str(k): int(v) for k, v in sorted(rows)}
        return out
    finally:
        con.close()
