"""Polymarket raw records to the unified schema (FR-09).

* Markets (Gamma): ``symbol`` is the token of the first outcome ("Yes" in yes/no markets),
  ``no_symbol`` the second; ``market_id`` is the condition id. Resolution comes from the final
  ``outcomePrices``: [1, 0] is YES, [0, 1] is NO, [0.5, 0.5] is a 50-50 split (SCALAR, 0.5),
  anything else on a closed market is ``closed`` without a result. A market whose UMA status
  history contains a dispute carries ``disputed = true`` (DQ-05).
* Trades (Data API v2, taker fills): outcome 0 prints are YES prices; outcome 1 (NO) prints
  become ``price = 1 - p`` with the side flipped (DQ-04). ``symbol`` is left null for NO
  prints and filled from the market in the warehouse. v2 has no log index, so ``trade_id`` is
  a hash of the transaction hash, token, side, size, price, time and wallet.
* Price history (CLOB): one ``close`` per point, ``interval_s`` = fidelity in seconds.
* Books (CLOB ``/book``): bids are side +1, asks side -1 (YES token book is canonical; a NO
  token book is stored under its own symbol for parity checks).
"""

from __future__ import annotations

from typing import Any

from pmr.normalize.categories import CategoryMap
from pmr.normalize.util import (
    fnum,
    iso_ns,
    json_list,
    rules_hash,
    stable_id,
    unix_ms_ns,
    unix_s_ns,
)

VENUE = "polymarket"


def _result(prices: list[float | None]) -> tuple[str | None, float | None]:
    if len(prices) != 2 or any(p is None for p in prices):
        return None, None
    a, b = prices
    if a == 1.0 and b == 0.0:
        return "YES", 1.0
    if a == 0.0 and b == 1.0:
        return "NO", 0.0
    if a == 0.5 and b == 0.5:
        return "SCALAR", 0.5
    return None, None


def market_row(m: dict[str, Any], ts_recv: int, cats: CategoryMap) -> dict[str, Any] | None:
    """One Gamma market to a ``markets`` row (None if it has no tradable tokens)."""
    tokens = [str(t) for t in json_list(m.get("clobTokenIds"))]
    outcomes = [str(o) for o in json_list(m.get("outcomes"))]
    if len(tokens) != 2:
        return None
    prices = [fnum(p) for p in json_list(m.get("outcomePrices"))]
    closed = bool(m.get("closed"))
    result, settle = _result(prices) if closed else (None, None)
    uma = [str(s).lower() for s in json_list(m.get("umaResolutionStatuses"))]
    disputed = any("disput" in s for s in uma)
    if result is not None:
        status = "resolved"
    elif closed and disputed:
        status = "disputed"
    elif closed:
        status = "closed"
    else:
        status = "open"
    events = m.get("events") or []
    ev = events[0] if events and isinstance(events[0], dict) else {}
    tags = [t.get("label") for t in (m.get("tags") or []) if isinstance(t, dict)]
    ev_tags = [t.get("label") for t in (ev.get("tags") or []) if isinstance(t, dict)]
    labels = [str(x) for x in [*tags, *ev_tags, m.get("category"), ev.get("category")] if x]
    rules = m.get("description")
    close_ts = iso_ns(m.get("closedTime")) or iso_ns(m.get("endDate"))
    return {
        "venue": VENUE,
        "symbol": tokens[0],
        "market_id": str(m.get("conditionId") or m.get("id")),
        "event_id": str(ev["id"]) if ev.get("id") is not None else None,
        "series_id": None,
        "title": m.get("question"),
        "subtitle": m.get("groupItemTitle"),
        "outcome_label": outcomes[0] if outcomes else None,
        "rules_text": rules,
        "rules_secondary": None,
        "rules_hash": rules_hash(rules),
        "resolution_source": m.get("resolutionSource") or ev.get("resolutionSource") or None,
        "category_raw": "|".join(labels) if labels else None,
        "category": cats.map(VENUE, labels),
        "open_ts": iso_ns(m.get("startDate")) or iso_ns(m.get("createdAt")),
        "close_ts": close_ts,
        "expected_resolution_ts": iso_ns(m.get("endDate")),
        "resolved_ts": iso_ns(m.get("closedTime")) if result else None,
        "rules_tz": None,
        "status": status,
        "result": result,
        "settlement_value": settle,
        "tick_size": fnum(m.get("orderPriceMinTickSize")),
        "lot_size": fnum(m.get("orderMinSize")),
        "contract_type": "binary_event",
        "currency": "USDC",
        "is_play_money": False,
        "is_forecast": False,
        "fee_schedule_id": None,
        "no_symbol": tokens[1],
        "disputed": disputed,
        "n_traders": None,
        "volume": fnum(m.get("volumeNum"))
        if m.get("volumeNum") is not None
        else fnum(m.get("volume")),
        "valid_from": ts_recv,
        "ingested_at": ts_recv,
    }


def markets(rec: dict[str, Any], cats: CategoryMap) -> list[dict[str, Any]]:
    """Rows from one Gamma keyset page."""
    body = rec.get("body")
    items = body.get("markets", []) if isinstance(body, dict) else body or []
    rows = [market_row(m, int(rec["ts_recv_ns"]), cats) for m in items if isinstance(m, dict)]
    return [r for r in rows if r is not None]


def trades(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from one Data API v2 trades page."""
    body = rec.get("body")
    items = body.get("data", []) if isinstance(body, dict) else []
    out: list[dict[str, Any]] = []
    cid = (rec.get("key") or {}).get("condition_id")
    for t in items:
        if not isinstance(t, dict):
            continue
        price = fnum(t.get("price"))
        size = fnum(t.get("size"))
        if price is None or size is None:
            continue
        idx = t.get("outcome_index")
        buy = str(t.get("side", "")).upper() == "BUY"
        is_no = idx == 1
        p_yes = 1.0 - price if is_no else price
        side = (1 if buy else -1) * (-1 if is_no else 1)
        token = str(t.get("token_id") or "")
        out.append(
            {
                "ts_exchange": unix_s_ns(t.get("timestamp")),
                "ts_recv": int(rec["ts_recv_ns"]),
                "venue": VENUE,
                "symbol": None if is_no else token,
                "trade_id": stable_id(
                    t.get("transaction_hash"),
                    token,
                    t.get("side"),
                    t.get("size"),
                    t.get("price"),
                    t.get("timestamp"),
                    t.get("proxy_wallet"),
                ),
                "price": p_yes,
                "qty": size,
                "side": side,
                "market_id": str(t.get("condition_id") or cid or ""),
                "source": "api",
                "notional": price * size,
            }
        )
    return out


def price_history(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from one CLOB prices-history response."""
    body = rec.get("body")
    hist = body.get("history", []) if isinstance(body, dict) else []
    key = rec.get("key") or {}
    token = str(key.get("token_id") or "")
    interval = int(key.get("fidelity_min") or 0) * 60
    out: list[dict[str, Any]] = []
    for h in hist:
        if not isinstance(h, dict) or h.get("t") is None or fnum(h.get("p")) is None:
            continue
        out.append(
            {
                "venue": VENUE,
                "symbol": token,
                "ts": unix_s_ns(h["t"]),
                "interval_s": interval,
                "close": fnum(h["p"]),
                "ingested_at": int(rec["ts_recv_ns"]),
            }
        )
    return out


def book(rec: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """Rows from one CLOB ``/book`` snapshot."""
    body = rec.get("body")
    if not isinstance(body, dict):
        return []
    token = str(body.get("asset_id") or (rec.get("key") or {}).get("token_id") or "")
    ts_ex = unix_ms_ns(body.get("timestamp")) if body.get("timestamp") else None
    out: list[dict[str, Any]] = []
    for side, levels in ((1, body.get("bids") or []), (-1, body.get("asks") or [])):
        for lvl in levels:
            px, qty = fnum(lvl.get("price")), fnum(lvl.get("size"))
            if px is None or qty is None:
                continue
            out.append(
                {
                    "ts_exchange": ts_ex,
                    "ts_recv": int(rec["ts_recv_ns"]),
                    "venue": VENUE,
                    "symbol": token,
                    "seq": seq,
                    "side": side,
                    "price": px,
                    "qty": qty,
                    "is_snapshot": True,
                }
            )
    return out
