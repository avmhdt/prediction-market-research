"""Polymarket ``markets`` and ``trades`` from the decoded on-chain tables (owner decision
2026-11-10; Polymarket's servers are not used for history).

* Each Polymarket condition (prepared by one of the allow-listed oracles) is a market. Its two
  outcome tokens come from the Conditional Tokens derivation (:mod:`pmr.normalize.ctf`) with
  USDC.e as collateral, or NegRisk's wrapped collateral when the oracle is the NegRiskAdapter.
  ``symbol`` is the outcome-0 token (the question's first label, "Yes") and ``no_symbol`` the
  outcome-1 token. Protocol V2 positions are ``condition | outcome`` (low byte).
* Title and rules come from UMA ancillary data (or NegRisk question data, or V2 rules JSON).
  Categories come from the title through the keyword rules (no tags exist on-chain).
* ``close_ts`` and ``resolved_ts`` are the on-chain resolution time (the exact trading close is
  not on-chain; Polymarket markets trade until they resolve). ``disputed`` is true when the UMA
  question was reset (a dispute).
* Fills are joined to tokens: outcome-1 prints become YES prices ``1 - p`` with the side
  flipped; ``trade_id`` is ``txhash:logIndex``; ``source = onchain``.
"""

from __future__ import annotations

import duckdb
import polars as pl

from pmr.normalize.categories import CategoryMap
from pmr.normalize.ctf import NEG_RISK_WCOL, USDC_E, binary_tokens
from pmr.normalize.schema import MARKETS
from pmr.normalize.util import rules_hash

NEG_RISK_ADAPTER = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"


def _latest(df: pl.DataFrame, key: str) -> pl.DataFrame:
    return df.sort(["ts", key], nulls_last=False).unique(
        subset=[key], keep="last", maintain_order=True
    )


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


def markets(
    tables: dict[str, pl.DataFrame], cats: CategoryMap
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``(markets rows, token map)`` for Polymarket from the on-chain tables."""
    cond = tables.get("oc_conditions")
    if cond is None or cond.is_empty():
        return pl.DataFrame(schema=MARKETS), pl.DataFrame()
    cond = cond.sort("ts").unique(subset=["condition_id"], keep="first", maintain_order=True)
    tmap = token_map(cond)
    q = tables.get(
        "oc_questions", pl.DataFrame(schema={"question_id": pl.String, "kind": pl.String})
    )
    text = q.filter(pl.col("kind").is_in(["uma", "negrisk"]) & pl.col("title").is_not_null())
    text = text.sort("ts").unique(subset=["question_id"], keep="last", maintain_order=True)
    resets = (
        set(q.filter(pl.col("kind") == "reset")["question_id"].to_list()) if q.height else set()
    )
    res = tables.get("oc_resolutions")
    res_map: dict[str, tuple[float, float, int]] = {}
    if res is not None and res.height:
        for cid, p0, p1, ts in (
            _latest(res, "condition_id")
            .select("condition_id", "payout0", "payout1", "ts")
            .iter_rows()
        ):
            res_map[cid] = (p0, p1, ts)
    tdict = {(c, o): t for t, c, o in tmap.iter_rows()}
    qdict = {r["question_id"]: r for r in text.iter_rows(named=True)}
    rows = []
    for cid, oracle, qid, slots, ts in cond.iter_rows():
        if slots != 2:
            continue
        info = qdict.get(qid, {})
        title, rules = info.get("title"), info.get("rules")
        result = settle = resolved = None
        status = "open"
        if cid in res_map:
            p0, p1, rts = res_map[cid]
            resolved = rts
            if (p0, p1) == (1.0, 0.0):
                result, settle = "YES", 1.0
            elif (p0, p1) == (0.0, 1.0):
                result, settle = "NO", 0.0
            else:
                result, settle = "SCALAR", p0
            status = "resolved"
        neg = oracle == NEG_RISK_ADAPTER
        event_id = (qid[:-2] + "00") if neg else cid
        rows.append(
            {
                "venue": "polymarket",
                "symbol": tdict[(cid, 0)],
                "market_id": cid,
                "event_id": event_id,
                "series_id": None,
                "title": title,
                "subtitle": None,
                "outcome_label": None,
                "rules_text": rules,
                "rules_secondary": None,
                "rules_hash": rules_hash(rules),
                "resolution_source": None,
                "category_raw": None,
                "category": cats.map("polymarket", [title or ""]),
                "open_ts": ts,
                "close_ts": resolved,
                "expected_resolution_ts": None,
                "resolved_ts": resolved,
                "rules_tz": None,
                "status": status,
                "result": result,
                "settlement_value": settle,
                "tick_size": None,
                "lot_size": None,
                "contract_type": "binary_event",
                "currency": "USDC",
                "is_play_money": False,
                "is_forecast": False,
                "fee_schedule_id": None,
                "no_symbol": tdict[(cid, 1)],
                "disputed": qid in resets,
                "n_traders": None,
                "volume": None,
                "valid_from": ts or 0,
                "ingested_at": ts or 0,
            }
        )
    out = pl.DataFrame({c: [r[c] for r in rows] for c in MARKETS}, schema=MARKETS)
    return out, tmap


def insert_trades(con: duckdb.DuckDBPyConnection, fills_sql: str, tmap: pl.DataFrame) -> None:
    """Insert on-chain fills (``fills_sql`` selects the ``oc_fills`` rows) as YES trades."""
    if tmap.is_empty():
        return
    con.register("oc_tmap", tmap.to_arrow())
    con.execute(
        f"""
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
        JOIN oc_tmap m ON f.token_id = m.token_id
        JOIN oc_tmap y ON y.condition_id = m.condition_id AND y.outcome = 0
        WHERE f.rn = 1
        ORDER BY m.condition_id, f.ts, f.trade_id
        """
    )
    con.unregister("oc_tmap")
