"""Unified schema (SPEC section 7b) as Polars schemas for the interim Parquet datasets.

All timestamps are int64 nanoseconds UTC. Prices are YES-orientation probabilities in [0, 1].
Columns after the canonical ones are additions documented in docs/data_dictionary.md.
"""

from __future__ import annotations

import polars as pl

MARKETS: dict[str, pl.DataType] = {
    "venue": pl.String(),
    "symbol": pl.String(),
    "market_id": pl.String(),
    "event_id": pl.String(),
    "series_id": pl.String(),
    "title": pl.String(),
    "subtitle": pl.String(),
    "outcome_label": pl.String(),
    "rules_text": pl.String(),
    "rules_secondary": pl.String(),
    "rules_hash": pl.String(),
    "resolution_source": pl.String(),
    "category_raw": pl.String(),
    "category": pl.String(),
    "open_ts": pl.Int64(),
    "close_ts": pl.Int64(),
    "expected_resolution_ts": pl.Int64(),
    "resolved_ts": pl.Int64(),
    "rules_tz": pl.String(),
    "status": pl.String(),
    "result": pl.String(),
    "settlement_value": pl.Float64(),
    "tick_size": pl.Float64(),
    "lot_size": pl.Float64(),
    "contract_type": pl.String(),
    "currency": pl.String(),
    "is_play_money": pl.Boolean(),
    "is_forecast": pl.Boolean(),
    "fee_schedule_id": pl.String(),
    "no_symbol": pl.String(),
    "disputed": pl.Boolean(),
    "n_traders": pl.Int64(),
    "volume": pl.Float64(),
    "valid_from": pl.Int64(),
    "ingested_at": pl.Int64(),
}

EVENTS: dict[str, pl.DataType] = {
    "venue": pl.String(),
    "event_id": pl.String(),
    "series_id": pl.String(),
    "title": pl.String(),
    "category_raw": pl.String(),
    "ingested_at": pl.Int64(),
}

TRADES: dict[str, pl.DataType] = {
    "ts_exchange": pl.Int64(),
    "ts_recv": pl.Int64(),
    "venue": pl.String(),
    "symbol": pl.String(),
    "trade_id": pl.String(),
    "price": pl.Float64(),
    "qty": pl.Float64(),
    "side": pl.Int8(),
    "market_id": pl.String(),
    "source": pl.String(),
    "notional": pl.Float64(),
}

PRICE_HISTORY: dict[str, pl.DataType] = {
    "venue": pl.String(),
    "symbol": pl.String(),
    "ts": pl.Int64(),
    "interval_s": pl.Int32(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "yes_bid_close": pl.Float64(),
    "yes_ask_close": pl.Float64(),
    "volume": pl.Float64(),
    "open_interest": pl.Float64(),
    "n_forecasters": pl.Int32(),
    "ingested_at": pl.Int64(),
}

BOOK_L2: dict[str, pl.DataType] = {
    "ts_exchange": pl.Int64(),
    "ts_recv": pl.Int64(),
    "venue": pl.String(),
    "symbol": pl.String(),
    "seq": pl.Int64(),
    "side": pl.Int8(),
    "price": pl.Float64(),
    "qty": pl.Float64(),
    "is_snapshot": pl.Boolean(),
}

RISK_FREE: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "series": pl.String(),
    "rate_pct": pl.Float64(),
    "ingested_at": pl.Int64(),
}

DATASETS: dict[str, dict[str, pl.DataType]] = {
    "markets": MARKETS,
    "events": EVENTS,
    "trades": TRADES,
    "price_history": PRICE_HISTORY,
    "book_l2": BOOK_L2,
    "risk_free": RISK_FREE,
}

STATUSES = ("open", "closed", "resolved", "voided", "disputed")
RESULTS = ("YES", "NO", "VOID", "SCALAR")


def frame(dataset: str, rows: list[dict[str, object]]) -> pl.DataFrame:
    """Build a DataFrame with exactly the dataset's columns and types (missing keys = null)."""
    schema = DATASETS[dataset]
    cols = {name: [r.get(name) for r in rows] for name in schema}
    return pl.DataFrame(cols, schema=schema)
