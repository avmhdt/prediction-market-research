-- Warehouse schema version 1 (SPEC section 7b). Applied by pmr.warehouse.build.
-- All timestamps are BIGINT nanoseconds UTC; prices are YES-orientation probabilities.

CREATE TABLE meta_schema (
    version INTEGER PRIMARY KEY,
    description VARCHAR NOT NULL
);

CREATE TABLE markets (
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    event_id VARCHAR,
    series_id VARCHAR,
    title VARCHAR,
    subtitle VARCHAR,
    outcome_label VARCHAR,
    rules_text VARCHAR,
    rules_secondary VARCHAR,
    rules_hash VARCHAR,
    resolution_source VARCHAR,
    category_raw VARCHAR,
    category VARCHAR NOT NULL,
    open_ts BIGINT,
    close_ts BIGINT,
    expected_resolution_ts BIGINT,
    resolved_ts BIGINT,
    rules_tz VARCHAR,
    status VARCHAR NOT NULL CHECK (status IN ('open', 'closed', 'resolved', 'voided', 'disputed')),
    result VARCHAR CHECK (result IS NULL OR result IN ('YES', 'NO', 'VOID', 'SCALAR')),
    settlement_value DOUBLE,
    tick_size DOUBLE,
    lot_size DOUBLE,
    contract_type VARCHAR NOT NULL,
    currency VARCHAR,
    is_play_money BOOLEAN NOT NULL,
    is_forecast BOOLEAN NOT NULL,
    fee_schedule_id VARCHAR,
    no_symbol VARCHAR,
    disputed BOOLEAN NOT NULL,
    n_traders BIGINT,
    volume DOUBLE,
    rules_changed BOOLEAN NOT NULL,
    valid_from BIGINT NOT NULL,
    valid_to BIGINT,
    ingested_at BIGINT NOT NULL,
    PRIMARY KEY (venue, symbol, valid_from)
);

CREATE TABLE events (
    venue VARCHAR NOT NULL,
    event_id VARCHAR,
    series_id VARCHAR,
    title VARCHAR,
    category_raw VARCHAR,
    ingested_at BIGINT NOT NULL
);

CREATE TABLE trades (
    ts_exchange BIGINT NOT NULL,
    ts_recv BIGINT NOT NULL,
    venue VARCHAR NOT NULL,
    symbol VARCHAR,
    trade_id VARCHAR NOT NULL,
    price DOUBLE NOT NULL CHECK (price >= 0 AND price <= 1),
    qty DOUBLE NOT NULL,
    side TINYINT NOT NULL CHECK (side IN (-1, 0, 1)),
    market_id VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    notional DOUBLE NOT NULL,
    PRIMARY KEY (venue, trade_id)
);

CREATE TABLE price_history (
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    ts BIGINT NOT NULL,
    interval_s INTEGER NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    yes_bid_close DOUBLE,
    yes_ask_close DOUBLE,
    volume DOUBLE,
    open_interest DOUBLE,
    n_forecasters INTEGER,
    PRIMARY KEY (venue, symbol, ts, interval_s)
);

CREATE TABLE book_l2 (
    ts_exchange BIGINT,
    ts_recv BIGINT NOT NULL,
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    seq BIGINT NOT NULL,
    side TINYINT NOT NULL CHECK (side IN (-1, 1)),
    price DOUBLE NOT NULL,
    qty DOUBLE NOT NULL,
    is_snapshot BOOLEAN NOT NULL
);

CREATE TABLE risk_free (
    date DATE NOT NULL,
    series VARCHAR NOT NULL,
    rate_pct DOUBLE NOT NULL,
    PRIMARY KEY (series, date)
);

INSERT INTO meta_schema VALUES (1, 'markets, events, trades, price_history, book_l2, risk_free');
