"""Polymarket ``markets`` and ``trades`` from the decoded on-chain tables (owner decision
2026-11-10; Polymarket's servers are not used for history). Everything runs in DuckDB so the
build stays inside its memory limit.

* Each Polymarket condition (prepared by one of the allow-listed oracles) is a market. Its two
  outcome tokens come from the Conditional Tokens derivation (:mod:`pmr.normalize.ctf`) with
  USDC.e as collateral, or NegRisk's wrapped collateral when the oracle is the NegRiskAdapter.
  Derived tokens are cached in ``<data>/processed/oc_token_map.parquet`` (only new conditions
  are derived on each build). ``symbol`` is the outcome-0 token (the question's first label,
  "Yes") and ``no_symbol`` the outcome-1 token.
* Title and rules come from UMA ancillary data or NegRisk question data. Categories come from
  the title through the keyword rules (no tags exist on-chain).
* ``close_ts`` and ``resolved_ts`` are the on-chain resolution time (the exact trading close is
  not on-chain; Polymarket markets trade until they resolve). ``disputed`` is true when the UMA
  question was reset (a dispute).
* Fills are joined to tokens: outcome-1 prints become YES prices ``1 - p`` with the side
  flipped; ``trade_id`` is ``txhash:logIndex``; ``source = onchain``; a fill already in
  ``trades`` (from the paper recorder) is not inserted twice.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl
import structlog

from pmr.normalize.categories import CategoryMap
from pmr.normalize.ctf import NEG_RISK_WCOL, USDC_E, binary_tokens
from pmr.normalize.store import interim_files

NEG_RISK_ADAPTER = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"
log = structlog.get_logger("pmr.warehouse")


def _files_sql(files: list[Path]) -> str:
    lst = "[" + ", ".join("'" + str(f).replace("'", "''") + "'" for f in files) + "]"
    return f"read_parquet({lst}, hive_partitioning = false, union_by_name = true)"


def token_map(conditions: pl.DataFrame) -> pl.DataFrame:
    """``token_id -> (condition_id, outcome)`` for every binary condition."""
    rows: list[tuple[str, str, int]] = []
    for cid, oracle, slots in conditions.select("condition_id", "oracle", "slots").iter_rows():
        if slots != 2:
            continue
        coll = NEG_RISK_WCOL if oracle == NEG_RISK_ADAPTER else USDC_E
        t0, t1 = binary_tokens(cid, coll)
        rows += [(str(t0), cid, 0), (str(t1), cid, 1)]
    return pl.DataFrame(
        rows,
        schema={"token_id": pl.String, "condition_id": pl.String, "outcome": pl.Int8},
        orient="row",
    )


TMAP_EMPTY = (
    "(SELECT NULL::VARCHAR AS token_id, NULL::VARCHAR AS condition_id, "
    "NULL::TINYINT AS outcome WHERE false)"
)


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "processed" / "oc_token_map.parquet"


def _cache_sql(data_dir: Path) -> str:
    cache = _cache_path(data_dir)
    if not cache.exists():
        return TMAP_EMPTY
    return "read_parquet('" + str(cache).replace("'", "''") + "')"


def update_token_cache(con: duckdb.DuckDBPyConnection, data_dir: Path, cond_sql: str) -> None:
    """Derive tokens for binary conditions not yet in the cache and append them (the cache
    holds ``token_id -> (condition_id, outcome)`` for every condition seen so far)."""
    cache = _cache_path(data_dir)
    todo = con.execute(
        f"""
        SELECT condition_id, arg_min(oracle, ts) AS oracle, arg_min(slots, ts) AS slots
        FROM {cond_sql} c
        WHERE NOT EXISTS (
            SELECT 1 FROM {_cache_sql(data_dir)} k WHERE k.condition_id = c.condition_id
        )
        GROUP BY condition_id HAVING arg_min(slots, ts) = 2
        """
    ).fetchall()
    fresh = token_map(
        pl.DataFrame(
            todo,
            schema={"condition_id": pl.String, "oracle": pl.String, "slots": pl.Int64},
            orient="row",
        )
    )
    if fresh.height:
        known = (
            pl.read_parquet(cache)
            if cache.exists()
            else pl.DataFrame(
                schema={"token_id": pl.String, "condition_id": pl.String, "outcome": pl.Int8}
            )
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        pl.concat([known, fresh], how="vertical").write_parquet(tmp, compression="zstd")
        tmp.replace(cache)
    log.info("onchain_token_map", derived=fresh.height)


def load_token_map(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    bucket: tuple[int, int] | None = None,
    name: str = "oc_tmap",
) -> None:
    """Create temp table ``name`` from the token cache: every token, or only the tokens of the
    conditions whose outcome-0 token (the market symbol) is in hash bucket ``(b, n)``."""
    src = _cache_sql(data_dir)
    where = ""
    if bucket is not None:
        b, n = bucket
        where = (
            f"WHERE condition_id IN (SELECT condition_id FROM {src} "
            f"WHERE outcome = 0 AND hash(token_id) % {int(n)} = {int(b)})"
        )
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM {src} {where}")


def stage_markets(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    cats: CategoryMap,
    bucket: tuple[int, int] | None = None,
) -> int:
    """Append Polymarket markets from the on-chain tables to the ``m_raw`` stage (all, or the
    markets in hash bucket ``(b, n)`` of their symbol); returns the number staged."""
    cond_files = interim_files(data_dir, "oc_conditions")
    if not cond_files:
        con.execute(f"CREATE OR REPLACE TEMP TABLE oc_tmap AS SELECT * FROM {TMAP_EMPTY}")
        return 0
    cond_sql = _files_sql(cond_files)
    update_token_cache(con, data_dir, cond_sql)
    load_token_map(con, data_dir, bucket)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE oc_cond AS
        SELECT condition_id, arg_min(oracle, ts) AS oracle,
               arg_min(question_id, ts) AS question_id, arg_min(slots, ts) AS slots,
               min(ts) AS ts
        FROM {cond_sql}
        WHERE condition_id IN (SELECT condition_id FROM oc_tmap)
        GROUP BY condition_id
        """
    )
    q_files = interim_files(data_dir, "oc_questions")
    r_files = interim_files(data_dir, "oc_resolutions")
    q_src = (
        _files_sql(q_files)
        if q_files
        else (
            "(SELECT NULL::VARCHAR AS question_id, NULL::VARCHAR AS kind, NULL::VARCHAR AS title, "
            "NULL::VARCHAR AS rules, NULL::BIGINT AS ts WHERE false)"
        )
    )
    r_src = (
        _files_sql(r_files)
        if r_files
        else (
            "(SELECT NULL::VARCHAR AS condition_id, NULL::DOUBLE AS payout0, "
            "NULL::DOUBLE AS payout1, NULL::BIGINT AS ts WHERE false)"
        )
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE oc_text AS
        SELECT question_id, arg_max(title, ts) AS title, arg_max(rules, ts) AS rules
        FROM {q_src} WHERE kind IN ('uma', 'negrisk') AND title IS NOT NULL
          AND question_id IN (SELECT question_id FROM oc_cond)
        GROUP BY question_id;
        CREATE OR REPLACE TEMP TABLE oc_reset AS
        SELECT DISTINCT question_id FROM {q_src} WHERE kind = 'reset'
          AND question_id IN (SELECT question_id FROM oc_cond);
        CREATE OR REPLACE TEMP TABLE oc_res AS
        SELECT condition_id, arg_max(payout0, ts) AS p0, arg_max(payout1, ts) AS p1,
               max(ts) AS rts
        FROM {r_src} WHERE condition_id IN (SELECT condition_id FROM oc_cond)
        GROUP BY condition_id;
        """
    )
    titles = [t for (t,) in con.execute("SELECT DISTINCT title FROM oc_text").fetchall()]
    cat_df = pl.DataFrame(
        {"title": titles, "cat": [cats.map("polymarket", [t or ""]) for t in titles]},
        schema={"title": pl.String, "cat": pl.String},
    )
    con.register("oc_titlecat", cat_df.to_arrow())
    before = con.execute("SELECT count(*) FROM m_raw").fetchone()
    con.execute(
        f"""
        INSERT INTO m_raw
        SELECT 'polymarket', y.token_id, c.condition_id,
               CASE WHEN c.oracle = '{NEG_RISK_ADAPTER}' THEN left(c.question_id, 64) || '00'
                    ELSE c.condition_id END,
               NULL, q.title, NULL, NULL, q.rules, NULL,
               left(sha256(coalesce(q.rules, '')), 16), NULL, NULL,
               coalesce(tc.cat, 'other'),
               c.ts, r.rts, NULL, r.rts, NULL,
               CASE WHEN r.condition_id IS NULL THEN 'open' ELSE 'resolved' END,
               CASE WHEN r.condition_id IS NULL THEN NULL
                    WHEN r.p0 = 1 AND r.p1 = 0 THEN 'YES'
                    WHEN r.p0 = 0 AND r.p1 = 1 THEN 'NO' ELSE 'SCALAR' END,
               CASE WHEN r.condition_id IS NULL THEN NULL
                    WHEN r.p0 = 1 AND r.p1 = 0 THEN 1.0
                    WHEN r.p0 = 0 AND r.p1 = 1 THEN 0.0 ELSE r.p0 END,
               NULL, NULL, 'binary_event', 'USDC', false, false, NULL, n.token_id,
               rs.question_id IS NOT NULL, NULL, NULL,
               coalesce(c.ts, 0), coalesce(c.ts, 0)
        FROM oc_cond c
        JOIN oc_tmap y ON y.condition_id = c.condition_id AND y.outcome = 0
        JOIN oc_tmap n ON n.condition_id = c.condition_id AND n.outcome = 1
        LEFT JOIN oc_text q ON q.question_id = c.question_id
        LEFT JOIN oc_titlecat tc ON tc.title = q.title
        LEFT JOIN oc_res r ON r.condition_id = c.condition_id
        LEFT JOIN oc_reset rs ON rs.question_id = c.question_id
        WHERE c.slots = 2
        """
    )
    con.unregister("oc_titlecat")
    after = con.execute("SELECT count(*) FROM m_raw").fetchone()
    staged = int(after[0]) - int(before[0]) if after and before else 0
    tok_files = interim_files(data_dir, "oc_tokens")
    if tok_files and (bucket is None or bucket[0] == 0):
        row = con.execute(
            f"""
            WITH reg AS (
                SELECT condition_id, any_value(least(token_a, token_b)) AS lo,
                       any_value(greatest(token_a, token_b)) AS hi
                FROM {_files_sql(tok_files)} GROUP BY condition_id
            ), der AS (
                SELECT condition_id, min(token_id) AS lo, max(token_id) AS hi
                FROM {_cache_sql(data_dir)} GROUP BY condition_id
            )
            SELECT count(*), count(*) FILTER (WHERE reg.lo <> der.lo OR reg.hi <> der.hi)
            FROM reg JOIN der USING (condition_id)
            """
        ).fetchone()
        if row:
            log.info("onchain_token_check", conditions=int(row[0]), mismatches=int(row[1]))
    return staged


def check_tokens(tmap: pl.DataFrame, registered: pl.DataFrame) -> tuple[int, int]:
    """(conditions checked, mismatches): derived tokens against exchange registrations."""
    if registered.is_empty() or tmap.is_empty():
        return 0, 0
    reg = registered.unique(subset=["condition_id"])
    derived = tmap.group_by("condition_id").agg(pl.col("token_id").sort().alias("tokens"))
    j = reg.join(derived, on="condition_id", how="inner")
    bad = sum(
        1
        for a, b, toks in j.select("token_a", "token_b", "tokens").iter_rows()
        if sorted([a, b]) != list(toks)
    )
    return j.height, bad


def ensure_token_map(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    """Make sure ``oc_tmap_full`` (every token in the cache) exists for the trades steps."""
    has = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'oc_tmap_full'"
    ).fetchone()
    if not (has and has[0]):
        load_token_map(con, data_dir, None, name="oc_tmap_full")


def insert_trades_sql(fills_sql: str) -> str:
    """SQL inserting on-chain fills (``fills_sql`` selects ``oc_fills`` rows) as YES trades,
    using the ``oc_tmap_full`` token map; a fill already in ``trades`` is not inserted
    twice."""
    return f"""
        INSERT INTO trades
        SELECT f.ts, f.ingested_at, 'polymarket', y.token_id, f.trade_id,
               CASE WHEN m.outcome = 0 THEN f.price ELSE 1 - f.price END,
               f.qty,
               CASE WHEN m.outcome = 0 THEN f.side ELSE -f.side END,
               m.condition_id, 'onchain', f.price * f.qty
        FROM (
            SELECT *, row_number() OVER (PARTITION BY trade_id ORDER BY ingested_at) AS rn
            FROM ({fills_sql})
        ) f
        JOIN oc_tmap_full m ON f.token_id = m.token_id
        JOIN oc_tmap_full y ON y.condition_id = m.condition_id AND y.outcome = 0
        WHERE f.rn = 1
          AND NOT EXISTS (
              SELECT 1 FROM trades t WHERE t.venue = 'polymarket' AND t.trade_id = f.trade_id
          )
        """


def insert_trades(con: duckdb.DuckDBPyConnection, fills_sql: str, data_dir: Path) -> None:
    """Insert on-chain fills as YES trades (see :func:`insert_trades_sql`)."""
    ensure_token_map(con, data_dir)
    con.execute(insert_trades_sql(fills_sql))
