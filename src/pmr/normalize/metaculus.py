"""Metaculus posts to the unified schema (FR-07). Forecasts, not prices: ``is_forecast``.

Each binary post becomes a ``markets`` row (resolution ``yes``/``no`` -> YES/NO,
``annulled``/``ambiguous`` -> VOID) and its community-prediction history, when the account tier
returns it, becomes ``price_history`` rows (close = centre of the recency-weighted aggregate,
``n_forecasters`` = forecaster count, ``ts`` = the start of each history interval, the moment
the aggregate became public, so no snapshot sees it early; DQ-02).
"""

from __future__ import annotations

from typing import Any

from pmr.normalize.categories import CategoryMap
from pmr.normalize.util import fnum, iso_ns, rules_hash, unix_s_ns

VENUE = "metaculus"


def _post_rows(
    p: dict[str, Any], ts_recv: int, cats: CategoryMap
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    q = p.get("question") or {}
    if not isinstance(q, dict) or q.get("type") != "binary":
        return None, []
    res = str(q.get("resolution") or "").lower()
    result, settle = {"yes": ("YES", 1.0), "no": ("NO", 0.0)}.get(res, (None, None))
    if res in ("annulled", "ambiguous"):
        result = "VOID"
    status = (
        "resolved"
        if result in ("YES", "NO")
        else ("voided" if result == "VOID" else str(q.get("status") or "closed"))
    )
    if status not in ("open", "closed", "resolved", "voided"):
        status = "closed"
    cats_raw = [
        str(c.get("name") or c.get("slug"))
        for c in ((p.get("projects") or {}).get("category") or [])
        if isinstance(c, dict)
    ]
    rules = q.get("resolution_criteria") or q.get("description")
    rules2 = q.get("fine_print")
    qid = str(q.get("id") or p.get("id"))
    row = {
        "venue": VENUE,
        "symbol": qid,
        "market_id": qid,
        "event_id": str(p.get("id")),
        "series_id": None,
        "title": q.get("title") or p.get("title"),
        "subtitle": None,
        "outcome_label": "YES",
        "rules_text": rules,
        "rules_secondary": rules2,
        "rules_hash": rules_hash(rules, rules2),
        "resolution_source": None,
        "category_raw": "|".join(cats_raw) if cats_raw else None,
        "category": cats.map(VENUE, cats_raw),
        "open_ts": iso_ns(q.get("open_time")),
        "close_ts": iso_ns(q.get("actual_close_time")) or iso_ns(q.get("scheduled_close_time")),
        "expected_resolution_ts": iso_ns(q.get("scheduled_resolve_time")),
        "resolved_ts": iso_ns(q.get("actual_resolve_time")),
        "rules_tz": None,
        "status": status,
        "result": result,
        "settlement_value": settle,
        "tick_size": None,
        "lot_size": None,
        "contract_type": "binary_event",
        "currency": None,
        "is_play_money": False,
        "is_forecast": True,
        "fee_schedule_id": None,
        "no_symbol": None,
        "disputed": False,
        "n_traders": int(p["nr_forecasters"]) if p.get("nr_forecasters") is not None else None,
        "volume": None,
        "valid_from": ts_recv,
        "ingested_at": ts_recv,
    }
    hist = (((q.get("aggregations") or {}).get("recency_weighted") or {}).get("history")) or []
    ticks: list[dict[str, Any]] = []
    for h in hist:
        if not isinstance(h, dict):
            continue
        centers = h.get("centers") or []
        if not centers or fnum(centers[0]) is None:
            continue
        t = h.get("start_time")
        ticks.append(
            {
                "venue": VENUE,
                "symbol": qid,
                "ts": unix_s_ns(t),
                "interval_s": 0,
                "close": fnum(centers[0]),
                "n_forecasters": h.get("forecaster_count"),
                "ingested_at": ts_recv,
            }
        )
    return row, ticks


def posts(
    rec: dict[str, Any], cats: CategoryMap
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(markets, price_history)`` rows from one posts page."""
    body = rec.get("body")
    items = body.get("results", []) if isinstance(body, dict) else []
    markets: list[dict[str, Any]] = []
    ticks: list[dict[str, Any]] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        row, t = _post_rows(p, int(rec["ts_recv_ns"]), cats)
        if row is not None:
            markets.append(row)
            ticks.extend(t)
    return markets, ticks
