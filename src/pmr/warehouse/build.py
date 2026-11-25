"""DuckDB warehouse: build from interim Parquet, migrate, stats and checksums (FR-10, AC-W-04).

``build`` starts from an empty file (written to ``*.tmp`` and renamed), applies ``ddl.sql``
and every ``migrations/NNN_*.sql``, loads the interim datasets and creates the views; a build
that was stopped resumes from its last committed step when the interim files are unchanged.
The result depends only on the interim files, which depend only on the raw archive, so two
builds give identical per-table checksums.

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

import contextlib
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import duckdb
import polars as pl
import structlog

from pmr.normalize.categories import CategoryMap
from pmr.normalize.schema import MARKETS
from pmr.normalize.store import interim_files
from pmr.warehouse import onchain
from pmr.warehouse.guard import BuildStopped, Guard

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


TEMP_LIMIT = "1GB"  # owner decision 2026-11-24: no multi-GB spill bursts on the shared drive


def temp_limit() -> str:
    """DuckDB spill cap: ``PMR_DUCKDB_TEMP_MAX`` (for example ``2GB``) or :data:`TEMP_LIMIT`."""
    v = os.environ.get("PMR_DUCKDB_TEMP_MAX", "").strip().upper()
    return v if re.fullmatch(r"\d+(\.\d+)?(MB|GB)", v) else TEMP_LIMIT


def _connect(path: Path, threads: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(f"SET threads = {int(threads)}")
    con.execute(f"SET memory_limit = '{memory_limit()}'")
    spill = path.parent / "duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{str(spill).replace(chr(39), chr(39) * 2)}'")
    con.execute(f"SET max_temp_directory_size = '{temp_limit()}'")
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


def version_markets_sql(con: duckdb.DuckDBPyConnection, source: str) -> str:
    """SQL selecting Type-2 market versions from table ``source`` (MARKETS columns plus
    ``rules_changed`` and ``valid_to``).

    A lite record is dropped when a full record (with rules text) of the same market exists;
    duplicate snapshots at the same ``valid_from`` keep one row; consecutive identical versions
    collapse; ``valid_to`` is the next version's ``valid_from``; ``rules_changed`` flags a
    version whose rules differ from the first version's (DQ-03).

    The window work runs on a narrow key table (row id, market, time, content hash) staged here
    as ``_vk``; the returned SQL joins the kept rows back to ``source``. Windows over the full
    rows (rules text included) did not fit the memory limit on the full data.
    """
    content = ", ".join(f"coalesce(CAST({c} AS VARCHAR), chr(0))" for c in VERSION_COLS)
    cols = ", ".join(f"s.{c}" for c in MARKETS)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE _vk0 AS
        SELECT rowid AS rid, venue, symbol, valid_from, rules_hash,
               rules_text IS NOT NULL AS has_rules,
               md5_number(concat_ws(chr(31), {content})) AS h
        FROM {source}
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE _vk AS
        WITH fullness AS (
            SELECT venue, symbol, bool_or(has_rules) AS any_full
            FROM _vk0 GROUP BY venue, symbol
        ), f AS (
            SELECT k.* FROM _vk0 k JOIN fullness USING (venue, symbol)
            WHERE NOT any_full OR has_rules
        ), d AS (
            SELECT * FROM f
            QUALIFY row_number() OVER (
                PARTITION BY venue, symbol, valid_from ORDER BY h, rid
            ) = 1
        ), l AS (
            SELECT *, lag(h) OVER (PARTITION BY venue, symbol ORDER BY valid_from) AS prev
            FROM d
        ), k AS (
            SELECT * FROM l WHERE prev IS NULL OR h <> prev
        ), v AS (
            SELECT rid, rules_hash,
                   first_value(rules_hash) OVER (
                       PARTITION BY venue, symbol ORDER BY valid_from
                   ) AS first_rules,
                   lead(valid_from) OVER (PARTITION BY venue, symbol ORDER BY valid_from)
                       AS valid_to
            FROM k
        )
        SELECT rid,
               coalesce(rules_hash IS NOT NULL AND first_rules IS NOT NULL
                        AND rules_hash <> first_rules, false) AS rules_changed,
               valid_to
        FROM v
        """
    )
    con.execute("DROP TABLE _vk0")
    return f"""
        SELECT {cols}, k.rules_changed, k.valid_to
        FROM _vk k JOIN {source} s ON s.rowid = k.rid
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
    """An empty staging table with the MARKETS columns and types (on disk, dropped after use)."""
    typed = ", ".join(f"{c} {DUCK_TYPES[type(t)]}" for c, t in MARKETS.items())
    con.execute(f"CREATE OR REPLACE TABLE {name} ({typed})")


def load_events(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    """Insert the distinct event and series records."""
    ev_files = interim_files(data_dir, "events")
    if ev_files:
        con.execute(
            f"""
            INSERT INTO events
            SELECT DISTINCT venue, event_id, series_id, title, category_raw, ingested_at
            FROM read_parquet({_parquet_list(ev_files)}, hive_partitioning = false)
            """
        )


def load_markets(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    cats: CategoryMap,
    bucket: tuple[int, int] | None = None,
) -> None:
    """Stage market snapshots (all, or one ``(b, n)`` hash bucket of ``symbol``), enrich and
    categorize in SQL, insert Type-2 versions. Needs ``events`` loaded first."""
    cols = ", ".join(MARKETS)
    where = f"WHERE {_bucket('symbol', *bucket)}" if bucket else ""
    create_markets_stage(con)
    files = interim_files(data_dir, "markets")
    if files:
        con.execute(
            f"INSERT INTO m_raw SELECT {cols} FROM read_parquet({_parquet_list(files)}, "
            f"hive_partitioning = false, union_by_name = true) {where}"
        )
    onchain.stage_markets(con, data_dir, cats, bucket)
    # Kalshi: series from the event, category from the series (else the event).
    for stmt in (
        """CREATE OR REPLACE TEMP TABLE ev_latest AS
           SELECT venue, event_id, arg_max(series_id, ingested_at) AS series_id,
                  arg_max(category_raw, ingested_at) AS cat
           FROM events WHERE event_id IS NOT NULL GROUP BY venue, event_id""",
        """CREATE OR REPLACE TEMP TABLE se_latest AS
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
        f"INSERT INTO markets ({cols}, rules_changed, valid_to) "
        + version_markets_sql(con, "m_raw")
    )
    con.execute("DROP TABLE _vk")
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
    """Row counts per table after a build; ``complete`` is false when the run stopped after its
    step budget (the partial build stays in ``*.tmp`` for the next run)."""

    path: Path
    rows: dict[str, int]
    complete: bool = True
    steps_run: tuple[str, ...] = ()
    steps_left: int = 0


# Large steps run in hash buckets of their partition key, so each committed step fits the
# memory limit without spilling and writes 0.5–2 GB (owner decision 2026-11-24, plan A).
BUCKETS = {"markets": 12, "trades": 48, "oc_trades": 8, "price_history": 4, "book_l2": 8}


def inputs_fingerprint(data_dir: Path) -> str:
    """Hash of the interim file list (paths and sizes); a resumed build needs the same inputs."""
    h = hashlib.sha256()
    root = Path(data_dir) / "interim"
    if root.exists():
        for f in sorted(root.rglob("*.parquet")):
            h.update(f"{f.relative_to(root).as_posix()}:{f.stat().st_size}\n".encode())
    return h.hexdigest()


def _open_build(tmp: Path, threads: int, fp: str) -> tuple[duckdb.DuckDBPyConnection, bool]:
    """Open ``tmp``; resume it when it holds a build of the same inputs, else start empty."""
    if tmp.exists():
        con = _connect(tmp, threads)
        row = None
        with contextlib.suppress(duckdb.Error):
            row = con.execute("SELECT fingerprint FROM _build_meta").fetchone()
        if row and row[0] == fp:
            return con, True
        con.close()
    for f in (tmp, Path(str(tmp) + ".wal")):
        if f.exists():
            f.unlink()
    con = _connect(tmp, threads)
    con.execute(DDL.read_text(encoding="utf-8"))
    for mig in migrations():
        con.execute(mig.path.read_text(encoding="utf-8"))
    con.execute("CREATE TABLE _build_steps (step VARCHAR PRIMARY KEY, done_at TIMESTAMP)")
    con.execute("CREATE TABLE _build_meta (fingerprint VARCHAR)")
    con.execute("INSERT INTO _build_meta VALUES (?)", [fp])
    return con, False


def _bucket(col: str, b: int, n: int) -> str:
    return f"hash({col}) % {int(n)} = {int(b)}"


Action = str | Callable[[duckdb.DuckDBPyConnection], None]


class _Budget(Exception):
    """The run's step budget is spent."""


class _Steps:
    """Runs named steps, each in one transaction recorded in ``_build_steps``; skips steps
    already recorded; stops after ``max_steps`` new steps; checks the guard before each."""

    def __init__(
        self, con: duckdb.DuckDBPyConnection, max_steps: int | None, guard: Guard | None
    ) -> None:
        self.con = con
        self.max_steps = max_steps
        self.guard = guard
        self.run: list[str] = []
        self.skipped_left = 0

    def done(self, step: str) -> bool:
        row = self.con.execute(
            "SELECT count(*) FROM _build_steps WHERE step = ?", [step]
        ).fetchone()
        return bool(row and row[0])

    def __call__(self, step: str, *sql: Action) -> None:
        if self.done(step):
            return
        if self.max_steps is not None and len(self.run) >= self.max_steps:
            self.skipped_left += 1
            raise _Budget(step)
        if self.guard is not None:
            self.guard.raise_if_stopped()
        con = self.con
        con.execute("BEGIN TRANSACTION")
        try:
            for q in sql:
                if callable(q):
                    q(con)
                else:
                    con.execute(q)
            con.execute("INSERT INTO _build_steps VALUES (?, now())", [step])
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(duckdb.Error):
                con.execute("ROLLBACK")
            raise
        self.run.append(step)
        log.info("warehouse_step", step=step)


def _plan(data_dir: Path, cats: CategoryMap) -> list[tuple[str, tuple[Action, ...]]]:
    """Every build step in order, as ``(name, actions)``; an action is SQL or a function of
    the connection."""
    steps: list[tuple[str, tuple[Action, ...]]] = []
    steps.append(("events", (partial(load_events, data_dir=data_dir),)))
    n = BUCKETS["markets"]
    for b in range(n):
        steps.append(
            (
                f"markets:{b}/{n}",
                (partial(load_markets, data_dir=data_dir, cats=cats, bucket=(b, n)),),
            )
        )
    files = interim_files(data_dir, "trades")
    if files:
        n = BUCKETS["trades"]
        for b in range(n):
            steps.append(
                (
                    f"trades:{b}/{n}",
                    # Dedup the bucket first, then map only the markets its trades need (a
                    # missing symbol or market id): maps over all 25M current markets did not
                    # fit the memory limit next to the window.
                    (
                        f"""
                        CREATE OR REPLACE TEMP TABLE _tb AS
                        SELECT ts_exchange, ts_recv, venue, symbol, trade_id, price, qty, side,
                               market_id, source, notional
                        FROM (
                            SELECT *, row_number() OVER (
                                PARTITION BY venue, trade_id
                                ORDER BY {SOURCE_RANK}, ts_recv, ts_exchange, price, qty
                            ) AS rn
                            FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                            WHERE ts_exchange IS NOT NULL AND price BETWEEN 0 AND 1
                              AND {_bucket("trade_id", b, n)}
                        )
                        WHERE rn = 1
                        """,
                        """
                        CREATE OR REPLACE TEMP TABLE _tm AS
                        SELECT venue, market_id, min(symbol) AS symbol
                        FROM markets mk
                        WHERE valid_to IS NULL AND EXISTS (
                            SELECT 1 FROM _tb t
                            WHERE t.symbol IS NULL
                              AND t.venue = mk.venue AND t.market_id = mk.market_id
                        )
                        GROUP BY venue, market_id
                        """,
                        """
                        CREATE OR REPLACE TEMP TABLE _ts AS
                        SELECT venue, symbol, min(market_id) AS market_id
                        FROM markets mk
                        WHERE valid_to IS NULL AND EXISTS (
                            SELECT 1 FROM _tb t
                            WHERE (t.market_id IS NULL OR t.market_id = '')
                              AND t.venue = mk.venue AND t.symbol = mk.symbol
                        )
                        GROUP BY venue, symbol
                        """,
                        """
                        INSERT INTO trades
                        SELECT t.ts_exchange, t.ts_recv, t.venue, coalesce(t.symbol, m.symbol),
                               t.trade_id, t.price, t.qty, t.side,
                               coalesce(nullif(t.market_id, ''), ms.market_id, t.symbol),
                               t.source, t.notional
                        FROM _tb t
                        LEFT JOIN _tm m ON t.venue = m.venue AND t.market_id = m.market_id
                        LEFT JOIN _ts ms ON t.venue = ms.venue AND t.symbol = ms.symbol
                        """,
                        "DROP TABLE _tb",
                        "DROP TABLE _tm",
                        "DROP TABLE _ts",
                    ),
                )
            )
    files = interim_files(data_dir, "oc_fills")
    if files:
        n = BUCKETS["oc_trades"]
        src = f"SELECT * FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)"
        for b in range(n):
            steps.append(
                (
                    f"oc_trades:{b}/{n}",
                    (
                        partial(onchain.ensure_token_map, data_dir=data_dir),
                        onchain.insert_trades_sql(
                            f"SELECT * FROM ({src}) WHERE {_bucket('trade_id', b, n)}"
                        ),
                    ),
                )
            )
    files = interim_files(data_dir, "price_history")
    if files:
        n = BUCKETS["price_history"]
        for b in range(n):
            steps.append(
                (
                    f"price_history:{b}/{n}",
                    (
                        f"""
                        INSERT INTO price_history
                        SELECT venue, symbol, ts, interval_s, open, high, low, close,
                               yes_bid_close, yes_ask_close, volume, open_interest, n_forecasters
                        FROM (
                            SELECT *, row_number() OVER (
                                PARTITION BY venue, symbol, ts, interval_s
                                ORDER BY ingested_at DESC, close
                            ) AS rn
                            FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                            WHERE ts IS NOT NULL AND symbol IS NOT NULL
                              AND {_bucket("symbol", b, n)}
                        )
                        WHERE rn = 1
                        """,
                    ),
                )
            )
    files = interim_files(data_dir, "book_l2")
    if files:
        n = BUCKETS["book_l2"]
        for b in range(n):
            steps.append(
                (
                    f"book_l2:{b}/{n}",
                    (
                        f"""
                        INSERT INTO book_l2
                        SELECT ts_exchange, ts_recv, venue, symbol,
                               dense_rank() OVER (PARTITION BY venue, symbol ORDER BY ts_recv)
                                   AS seq,
                               side, price, qty, is_snapshot
                        FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                        WHERE symbol IS NOT NULL AND {_bucket("symbol", b, n)}
                        """,
                    ),
                )
            )
    files = interim_files(data_dir, "risk_free")
    if files:
        steps.append(
            (
                "risk_free",
                (
                    f"""
                    INSERT INTO risk_free
                    SELECT "date", series, rate_pct FROM (
                        SELECT *, row_number() OVER (
                            PARTITION BY series, "date" ORDER BY ingested_at DESC, rate_pct
                        ) AS rn
                        FROM read_parquet({_parquet_list(files)}, hive_partitioning = false)
                    ) WHERE rn = 1
                    """,
                ),
            )
        )
    return steps


def build(
    data_dir: Path,
    path: Path,
    cats: CategoryMap,
    *,
    threads: int = 8,
    max_steps: int | None = None,
    guard: Guard | None = None,
) -> BuildResult:
    """Build the warehouse at ``path`` from ``<data_dir>/interim``.

    Work goes to ``*.tmp`` and is renamed at the end. Each step (and each hash bucket of a
    large step) commits with a record in ``_build_steps``; a stopped build resumes from the
    last committed step when the interim files are unchanged, and starts over otherwise. With
    ``max_steps`` the run stops after that many new steps (``complete`` is then false). With a
    ``guard`` each step is checked before it starts and interrupted (rolled back) when a check
    fails, which raises :class:`~pmr.warehouse.guard.BuildStopped`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    con, resumed = _open_build(tmp, threads, inputs_fingerprint(data_dir))
    runner = _Steps(con, max_steps, guard)
    if resumed:
        done = [r[0] for r in con.execute("SELECT step FROM _build_steps").fetchall()]
        log.info("warehouse_resume", done=len(done))
    plan = _plan(data_dir, cats)
    watch = guard.watch(con) if guard is not None else contextlib.nullcontext()
    complete = False
    try:
        try:
            with watch:
                try:
                    for name, actions in plan:
                        runner(name, *actions)
                    complete = True
                except _Budget:
                    pass
                if not complete:
                    # Inside the watch: the checkpoint after a step writes too (the write cap).
                    con.execute("CHECKPOINT")
        except duckdb.Error as e:
            if guard is not None and guard.reason:
                raise BuildStopped(guard.reason) from e
            if "Too many open files" in str(e):
                # The process file limit, not the disk (cron's soft limit is 1024).
                raise BuildStopped(f"fd_limit: {e}") from e
            if isinstance(e, duckdb.IOException):
                raise BuildStopped(f"disk_error: {e}") from e
            raise
        except OSError as e:
            raise BuildStopped(f"disk_error: {e}") from e
        left = sum(1 for name, _ in plan if not runner.done(name))
        if not complete:
            return BuildResult(
                path=tmp, rows={}, complete=False, steps_run=tuple(runner.run), steps_left=left
            )
        con.execute(VIEWS.read_text(encoding="utf-8"))
        con.execute("DROP TABLE _build_steps")
        con.execute("DROP TABLE _build_meta")
        rows = table_counts(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, path)
    return BuildResult(path=path, rows=rows, complete=True, steps_run=tuple(runner.run))


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
