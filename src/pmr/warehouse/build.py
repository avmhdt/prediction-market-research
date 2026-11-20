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
import structlog

from pmr.normalize.categories import CategoryMap
from pmr.normalize.schema import MARKETS
from pmr.normalize.store import interim_files
from pmr.warehouse import onchain

log = structlog.get_logger("pmr.warehouse")

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


MEMORY_LIMIT = "12GB"  # the shared PC stops work below 30 GB free; DuckDB spills beyond this


def memory_limit() -> str:
    """DuckDB memory limit: ``PMR_DUCKDB_MEMORY`` (for example ``8GB``) or :data:`MEMORY_LIMIT`."""
    v = os.environ.get("PMR_DUCKDB_MEMORY", "").strip().upper()
    return v if re.fullmatch(r"\d+(\.\d+)?(MB|GB)", v) else MEMORY_LIMIT


def _connect(path: Path, threads: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(f"SET threads = {int(threads)}")
    con.execute(f"SET memory_limit = '{memory_limit()}'")
    spill = path.parent / "duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{str(spill).replace(chr(39), chr(39) * 2)}'")
    # Row order inside tables is irrelevant (checksums are order-independent), and keeping it
    # costs memory on large inserts.
    con.execute("SET preserve_insertion_order = false")
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


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def version_markets_sql(source: str) -> str:
    """SQL selecting Type-2 market versions from table ``source`` (MARKETS columns plus
    ``rules_changed`` and ``valid_to``).

    A lite record is dropped when a full record (with rules text) of the same market exists;
    duplicate snapshots at the same ``valid_from`` keep one row; consecutive identical versions
    collapse; ``valid_to`` is the next version's ``valid_from``; ``rules_changed`` flags a
    version whose rules differ from the first version's (DQ-03).
    """
    content = ", ".join(f"coalesce(CAST({c} AS VARCHAR), chr(0))" for c in VERSION_COLS)
    cols = ", ".join(MARKETS)
    return f"""
        WITH fullness AS (
            SELECT venue, symbol, bool_or(rules_text IS NOT NULL) AS any_full
            FROM {source} GROUP BY venue, symbol
        ), f AS (
            SELECT s.* FROM {source} s JOIN fullness USING (venue, symbol)
            WHERE NOT any_full OR rules_text IS NOT NULL
        ), h AS (
            SELECT *, md5(concat_ws(chr(31), {content})) AS _h FROM f
        ), d AS (
            SELECT * FROM h
            QUALIFY row_number() OVER (PARTITION BY venue, symbol, valid_from ORDER BY _h) = 1
        ), l AS (
            SELECT *, lag(_h) OVER (PARTITION BY venue, symbol ORDER BY valid_from) AS _prev
            FROM d
        ), k AS (
            SELECT * FROM l WHERE _prev IS NULL OR _h <> _prev
        ), v AS (
            SELECT *,
                   first_value(rules_hash) OVER (
                       PARTITION BY venue, symbol ORDER BY valid_from
                   ) AS _first_rules,
                   lead(valid_from) OVER (PARTITION BY venue, symbol ORDER BY valid_from)
                       AS _valid_to
            FROM k
        )
        SELECT {cols},
               coalesce(rules_hash IS NOT NULL AND _first_rules IS NOT NULL
                        AND rules_hash <> _first_rules, false) AS rules_changed,
               _valid_to AS valid_to
        FROM v
    """


DUCK_TYPES = {
    pl.String: "VARCHAR",
    pl.Int64: "BIGINT",
    pl.Int32: "INTEGER",
    pl.Int8: "TINYINT",
    pl.Float64: "DOUBLE",
    pl.Boolean: "BOOLEAN",
    pl.Date: "DATE",
}


def create_markets_stage(con: duckdb.DuckDBPyConnection, name: str = "m_raw") -> None:
    """An empty temp table with the MARKETS columns and types."""
    typed = ", ".join(f"{c} {DUCK_TYPES[type(t)]}" for c, t in MARKETS.items())
    con.execute(f"CREATE TEMP TABLE {name} ({typed})")


def load_markets(con: duckdb.DuckDBPyConnection, data_dir: Path, cats: CategoryMap) -> None:
    """Stage every market snapshot, enrich and categorize in SQL, insert Type-2 versions."""
    cols = ", ".join(MARKETS)
    create_markets_stage(con)
    files = interim_files(data_dir, "markets")
    if files:
        con.execute(
            f"INSERT INTO m_raw SELECT {cols} FROM read_parquet({_parquet_list(files)}, "
            "hive_partitioning = false, union_by_name = true)"
        )
    onchain.stage_markets(con, data_dir, cats)
    ev_files = interim_files(data_dir, "events")
    if ev_files:
        con.execute(
            f"""
            INSERT INTO events
            SELECT DISTINCT venue, event_id, series_id, title, category_raw, ingested_at
            FROM read_parquet({_parquet_list(ev_files)}, hive_partitioning = false)
            """
        )
        # Kalshi: series from the event, category from the series (else the event).
        for stmt in (
            """CREATE TEMP TABLE ev_latest AS
               SELECT venue, event_id, arg_max(series_id, ingested_at) AS series_id,
                      arg_max(category_raw, ingested_at) AS cat
               FROM events WHERE event_id IS NOT NULL GROUP BY venue, event_id""",
            """CREATE TEMP TABLE se_latest AS
               SELECT venue, series_id, arg_max(category_raw, ingested_at) AS cat
               FROM events WHERE event_id IS NULL GROUP BY venue, series_id""",
            """UPDATE m_raw SET series_id = e.series_id FROM ev_latest e
               WHERE m_raw.venue = 'kalshi' AND m_raw.series_id IS NULL
                 AND m_raw.venue = e.venue AND m_raw.event_id = e.event_id""",
            """UPDATE m_raw SET category_raw = s.cat FROM se_latest s
               WHERE m_raw.venue = 'kalshi' AND m_raw.category_raw IS NULL
                 AND m_raw.venue = s.venue AND m_raw.series_id = s.series_id""",
            """UPDATE m_raw SET category_raw = e.cat FROM ev_latest e
               WHERE m_raw.venue = 'kalshi' AND m_raw.category_raw IS NULL
                 AND m_raw.venue = e.venue AND m_raw.event_id = e.event_id""",
        ):
            con.execute(stmt)
    categorize(con, "m_raw", cats)
    con.execute(
        f"INSERT INTO markets ({cols}, rules_changed, valid_to) " + version_markets_sql("m_raw")
    )
    con.execute("DROP TABLE m_raw")


def categorize(con: duckdb.DuckDBPyConnection, table: str, cats: CategoryMap) -> None:
    """Fill null categories, mapping each distinct (venue, raw labels) once."""
    labels = con.execute(
        f"SELECT DISTINCT venue, category_raw FROM {table} WHERE category IS NULL"
    ).fetchall()
    if not labels:
        return
    mapped = pl.DataFrame(
        {
            "venue": [v for v, _ in labels],
            "category_raw": [r for _, r in labels],
            "cat": [cats.map(v, (r or "").split("|")) for v, r in labels],
        },
        schema={"venue": pl.String, "category_raw": pl.String, "cat": pl.String},
    )
    con.register("cat_map", mapped.to_arrow())
    con.execute(
        f"""
        UPDATE {table} SET category = c.cat FROM cat_map c
        WHERE {table}.category IS NULL AND {table}.venue = c.venue
          AND {table}.category_raw IS NOT DISTINCT FROM c.category_raw
        """
    )
    con.unregister("cat_map")


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

        load_markets(con, data_dir, cats)

        files = interim_files(data_dir, "trades")
        if files:
            con.execute(
                f"""
                INSERT INTO trades
                SELECT t.ts_exchange, t.ts_recv, t.venue, coalesce(t.symbol, m.symbol),
                       t.trade_id, t.price, t.qty, t.side,
                       coalesce(nullif(t.market_id, ''), ms.market_id, t.symbol),
                       t.source, t.notional
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
                LEFT JOIN (
                    SELECT venue, symbol, min(market_id) AS market_id
                    FROM markets WHERE valid_to IS NULL GROUP BY venue, symbol
                ) ms ON t.venue = ms.venue AND t.symbol = ms.symbol
                WHERE t.rn = 1
                """
            )
        files = interim_files(data_dir, "oc_fills")
        if files:
            onchain.insert_trades(
                con,
                f"SELECT * FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)",
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
