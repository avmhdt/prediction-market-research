"""Kalshi raw records to the unified schema (FR-09).

Kalshi reports prices either as integer cents (``yes_bid``) or as dollar strings
(``yes_bid_dollars``); the dollar field wins when present. Statuses map as
``initialized``/``unopened``/``active``/``open`` -> open, ``closed`` -> closed,
``determined``/``settled``/``finalized`` -> resolved (when the result is yes/no) or voided.
Order books list YES bids and NO bids only: a NO bid at q becomes a YES ask at 1 - q with the
same size (DQ-04). Trades: ``taker_side`` yes -> +1, no -> -1; the price is the YES price.
"""

from __future__ import annotations

from typing import Any

from pmr.normalize.categories import CategoryMap
from pmr.normalize.util import fnum, iso_ns, rules_hash, unix_s_ns

VENUE = "kalshi"
OPEN = {"initialized", "unopened", "active", "open"}
SETTLED = {"determined", "settled", "finalized"}


def dollars(obj: dict[str, Any], name: str) -> float | None:
    """Price in USD from ``<name>_dollars`` (string) or ``<name>`` (cents)."""
    d = fnum(obj.get(f"{name}_dollars"))
    if d is not None:
        return d
    c = fnum(obj.get(name))
    return None if c is None else c / 100.0


def market_row(m: dict[str, Any], ts_recv: int, cats: CategoryMap) -> dict[str, Any]:
    """One Kalshi market to a ``markets`` row (category filled from the series later)."""
    raw_status = str(m.get("status") or "").lower()
    res = str(m.get("result") or "").lower()
    result: str | None = None
    settle: float | None = None
    if res == "yes":
        result, settle = "YES", 1.0
    elif res == "no":
        result, settle = "NO", 0.0
    elif res in ("void", "voided", "all_no", "all_yes"):
        result = "VOID"
    elif res == "scalar":
        result = "SCALAR"
        settle = dollars(m, "settlement_value")
    if raw_status in OPEN:
        status = "open"
    elif raw_status in SETTLED:
        status = "voided" if result == "VOID" else ("resolved" if result else "closed")
    else:
        status = "closed"
    rules, rules2 = m.get("rules_primary"), m.get("rules_secondary")
    cat_raw = m.get("category")
    return {
        "venue": VENUE,
        "symbol": str(m.get("ticker")),
        "market_id": str(m.get("ticker")),
        "event_id": m.get("event_ticker"),
        "series_id": m.get("series_ticker"),
        "title": m.get("title"),
        "subtitle": m.get("subtitle") or m.get("yes_sub_title"),
        "outcome_label": m.get("yes_sub_title"),
        "rules_text": rules,
        "rules_secondary": rules2,
        "rules_hash": rules_hash(rules, rules2),
        "resolution_source": None,
        "category_raw": cat_raw,
        "category": cats.map(VENUE, [cat_raw]) if cat_raw else None,
        "open_ts": iso_ns(m.get("open_time")),
        "close_ts": iso_ns(m.get("close_time")),
        "expected_resolution_ts": iso_ns(m.get("expected_expiration_time"))
        or iso_ns(m.get("expiration_time")),
        "resolved_ts": iso_ns(m.get("settlement_ts")) or iso_ns(m.get("expiration_time"))
        if result
        else None,
        "rules_tz": None,
        "status": status,
        "result": result,
        "settlement_value": settle,
        "tick_size": dollars(m, "tick_size") if m.get("tick_size") is not None else 0.01,
        "lot_size": 1.0,
        "contract_type": "binary_event",
        "currency": "USD",
        "is_play_money": False,
        "is_forecast": False,
        "fee_schedule_id": None,
        "no_symbol": None,
        "disputed": False,
        "n_traders": None,
        "volume": fnum(m.get("volume_fp")) or fnum(m.get("volume")),
        "valid_from": ts_recv,
        "ingested_at": ts_recv,
    }


def markets(rec: dict[str, Any], cats: CategoryMap) -> list[dict[str, Any]]:
    """Rows from one /markets or /historical/markets page."""
    body = rec.get("body")
    items = body.get("markets", []) if isinstance(body, dict) else []
    return [market_row(m, int(rec["ts_recv_ns"]), cats) for m in items if isinstance(m, dict)]


def events(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from one /events page (event -> series and category)."""
    body = rec.get("body")
    items = body.get("events", []) if isinstance(body, dict) else []
    return [
        {
            "venue": VENUE,
            "event_id": e.get("event_ticker"),
            "series_id": e.get("series_ticker"),
            "title": e.get("title"),
            "category_raw": e.get("category"),
            "ingested_at": int(rec["ts_recv_ns"]),
        }
        for e in items
        if isinstance(e, dict)
    ]


def series(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Series rows go to ``events`` with ``event_id`` null (series -> category lookup)."""
    body = rec.get("body")
    items = body.get("series", []) if isinstance(body, dict) else []
    return [
        {
            "venue": VENUE,
            "event_id": None,
            "series_id": s.get("ticker"),
            "title": s.get("title"),
            "category_raw": s.get("category"),
            "ingested_at": int(rec["ts_recv_ns"]),
        }
        for s in items
        if isinstance(s, dict)
    ]


def trades(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from one live or historical trades page."""
    body = rec.get("body")
    items = body.get("trades", []) if isinstance(body, dict) else []
    source = "historical" if (rec.get("key") or {}).get("source") == "historical" else "api"
    out: list[dict[str, Any]] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        price = dollars(t, "yes_price")
        qty = fnum(t.get("count_fp")) or fnum(t.get("count"))
        if price is None or qty is None:
            continue
        taker = str(t.get("taker_side") or "").lower()
        out.append(
            {
                "ts_exchange": iso_ns(t.get("created_time")),
                "ts_recv": int(rec["ts_recv_ns"]),
                "venue": VENUE,
                "symbol": t.get("ticker"),
                "trade_id": str(t.get("trade_id")),
                "price": price,
                "qty": qty,
                "side": 1 if taker == "yes" else (-1 if taker == "no" else 0),
                "market_id": t.get("ticker"),
                "source": source,
                "notional": price * qty,
            }
        )
    return out


def candles(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from one candlesticks response (hourly bars, YES prices)."""
    body = rec.get("body")
    items = body.get("candlesticks", []) if isinstance(body, dict) else []
    key = rec.get("key") or {}
    ticker = key.get("ticker") or (body.get("ticker") if isinstance(body, dict) else None)
    out: list[dict[str, Any]] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        price = c.get("price") or {}
        bid = c.get("yes_bid") or {}
        ask = c.get("yes_ask") or {}
        out.append(
            {
                "venue": VENUE,
                "symbol": ticker,
                "ts": unix_s_ns(c.get("end_period_ts")),
                "interval_s": 3600,
                "open": dollars(price, "open"),
                "high": dollars(price, "high"),
                "low": dollars(price, "low"),
                "close": dollars(price, "close"),
                "yes_bid_close": dollars(bid, "close"),
                "yes_ask_close": dollars(ask, "close"),
                "volume": fnum(c.get("volume_fp")) or fnum(c.get("volume")),
                "open_interest": fnum(c.get("open_interest_fp")) or fnum(c.get("open_interest")),
                "ingested_at": int(rec["ts_recv_ns"]),
            }
        )
    return out


def book(rec: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """Rows from one order-book snapshot: YES bids (+1) and NO bids as YES asks (-1)."""
    body = rec.get("body")
    ob = body.get("orderbook", body) if isinstance(body, dict) else {}
    ob = ob or {}
    ticker = (rec.get("key") or {}).get("ticker")
    out: list[dict[str, Any]] = []

    def levels(name: str) -> list[tuple[float, float]]:
        d = ob.get(f"{name}_dollars")
        if d:
            return [(float(p), float(q)) for p, q in d]
        c = ob.get(name) or []
        return [(float(p) / 100.0, float(q)) for p, q in c]

    for px, qty in levels("yes"):
        out.append(_lvl(rec, ticker, seq, 1, px, qty))
    for px, qty in levels("no"):
        out.append(_lvl(rec, ticker, seq, -1, round(1.0 - px, 10), qty))
    return out


def _lvl(
    rec: dict[str, Any], ticker: Any, seq: int, side: int, px: float, qty: float
) -> dict[str, Any]:
    return {
        "ts_exchange": None,
        "ts_recv": int(rec["ts_recv_ns"]),
        "venue": VENUE,
        "symbol": ticker,
        "seq": seq,
        "side": side,
        "price": px,
        "qty": qty,
        "is_snapshot": True,
    }
