"""Manifold raw records to the unified schema (FR-06, FR-09). Play money: ``is_play_money``.

Markets come from the lite list (every market) and the full record (resolution, rules text,
groups). Resolutions: YES, NO, MKT (SCALAR, settled at ``resolutionProbability``),
CANCEL (VOID). Only binary markets are kept.

Bets become ``trades`` and a tick ``price_history`` (``interval_s = 0``, close =
``probAfter``). A bet's YES price is ``amount / shares`` for a YES bet and
``1 - amount / shares`` for a NO bet; selling (negative amount) flips the side. Redemptions,
cancelled and unfilled limit orders are skipped.
"""

from __future__ import annotations

from typing import Any

from pmr.normalize.categories import CategoryMap
from pmr.normalize.util import fnum, rules_hash, unix_ms_ns

VENUE = "manifold"
RESULTS = {"YES": ("YES", 1.0), "NO": ("NO", 0.0), "CANCEL": ("VOID", None)}


def market_row(m: dict[str, Any], ts_recv: int, cats: CategoryMap) -> dict[str, Any] | None:
    """One lite or full Manifold market to a ``markets`` row (None unless binary)."""
    if m.get("outcomeType") != "BINARY":
        return None
    res = m.get("resolution")
    result: str | None = None
    settle: float | None = None
    if res in RESULTS:
        result, settle = RESULTS[res]
    elif res == "MKT":
        result, settle = "SCALAR", fnum(m.get("resolutionProbability"))
    if m.get("isResolved"):
        status = "voided" if result == "VOID" else "resolved"
    elif m.get("closeTime") and int(m["closeTime"]) * 1_000_000 <= ts_recv:
        status = "closed"
    else:
        status = "open"
    groups = [str(g) for g in (m.get("groupSlugs") or [])]
    rules = m.get("textDescription")
    return {
        "venue": VENUE,
        "symbol": str(m["id"]),
        "market_id": str(m["id"]),
        "event_id": str(m["id"]),
        "series_id": None,
        "title": m.get("question"),
        "subtitle": None,
        "outcome_label": "YES",
        "rules_text": rules,
        "rules_secondary": None,
        "rules_hash": rules_hash(rules),
        "resolution_source": None,
        "category_raw": "|".join(groups) if groups else None,
        "category": cats.map(VENUE, groups),
        "open_ts": unix_ms_ns(m.get("createdTime")),
        "close_ts": unix_ms_ns(m.get("closeTime")),
        "expected_resolution_ts": unix_ms_ns(m.get("closeTime")),
        "resolved_ts": unix_ms_ns(m.get("resolutionTime")),
        "rules_tz": None,
        "status": status,
        "result": result,
        "settlement_value": settle,
        "tick_size": None,
        "lot_size": None,
        "contract_type": "binary_event",
        "currency": "MANA",
        "is_play_money": True,
        "is_forecast": False,
        "fee_schedule_id": None,
        "no_symbol": None,
        "disputed": False,
        "n_traders": int(m["uniqueBettorCount"])
        if m.get("uniqueBettorCount") is not None
        else None,
        "volume": fnum(m.get("volume")),
        "valid_from": ts_recv,
        "ingested_at": ts_recv,
    }


def markets(rec: dict[str, Any], cats: CategoryMap) -> list[dict[str, Any]]:
    """Rows from a lite list page or one full market record."""
    body = rec.get("body")
    items = body if isinstance(body, list) else [body]
    rows = [market_row(m, int(rec["ts_recv_ns"]), cats) for m in items if isinstance(m, dict)]
    return [r for r in rows if r is not None]


def bets(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(trades, price_history)`` rows from one bets page."""
    body = rec.get("body")
    items = body if isinstance(body, list) else []
    trades: list[dict[str, Any]] = []
    ticks: list[dict[str, Any]] = []
    ts_recv = int(rec["ts_recv_ns"])
    for b in items:
        if not isinstance(b, dict) or b.get("isRedemption") or b.get("isCancelled"):
            continue
        amount, shares = fnum(b.get("amount")), fnum(b.get("shares"))
        ts = unix_ms_ns(b.get("createdTime"))
        mid = str(b.get("contractId") or "")
        if ts is not None and fnum(b.get("probAfter")) is not None:
            ticks.append(
                {
                    "venue": VENUE,
                    "symbol": mid,
                    "ts": ts,
                    "interval_s": 0,
                    "close": fnum(b.get("probAfter")),
                    "ingested_at": ts_recv,
                }
            )
        if not amount or not shares:
            continue
        unit = abs(amount / shares)
        is_yes = str(b.get("outcome")).upper() == "YES"
        price = unit if is_yes else 1.0 - unit
        if not 0.0 <= price <= 1.0:
            continue
        side = (1 if is_yes else -1) * (1 if amount > 0 else -1)
        trades.append(
            {
                "ts_exchange": ts,
                "ts_recv": ts_recv,
                "venue": VENUE,
                "symbol": mid,
                "trade_id": str(b.get("id")),
                "price": price,
                "qty": abs(shares),
                "side": side,
                "market_id": mid,
                "source": "api",
                "notional": abs(amount),
            }
        )
    return trades, ticks
