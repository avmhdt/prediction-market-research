-- Views over the warehouse (FR-10). Recreated by every build and migrate.

-- Current version of each market (type-2 history: valid_to IS NULL).
CREATE OR REPLACE VIEW markets_current AS
SELECT * FROM markets WHERE valid_to IS NULL;

-- Best bid and offer per book snapshot (canonical bbo).
CREATE OR REPLACE VIEW bbo AS
SELECT
    max(ts_recv) AS ts,
    venue,
    symbol,
    seq,
    max(CASE WHEN side = 1 THEN price END) AS bid_px,
    arg_max(CASE WHEN side = 1 THEN qty END, CASE WHEN side = 1 THEN price END) AS bid_qty,
    min(CASE WHEN side = -1 THEN price END) AS ask_px,
    arg_min(CASE WHEN side = -1 THEN qty END, CASE WHEN side = -1 THEN price END) AS ask_qty
FROM book_l2
GROUP BY venue, symbol, seq;

-- Markets with a final YES/NO result (DQ-05), one row per market, with the play-money and
-- forecast flags kept so they are never pooled silently (DQ-10).
CREATE OR REPLACE VIEW resolved_binary AS
SELECT
    venue, symbol, market_id, event_id, category, close_ts, resolved_ts, open_ts,
    result, CASE WHEN result = 'YES' THEN 1 ELSE 0 END AS y,
    is_play_money, is_forecast, disputed, volume, n_traders
FROM markets_current
WHERE status = 'resolved' AND result IN ('YES', 'NO');

-- Trade counts and notional per market (inputs to the DQ-07 liquidity filter).
CREATE OR REPLACE VIEW market_liquidity AS
SELECT venue, market_id, count(*) AS n_trades, sum(notional) AS notional,
       min(ts_exchange) AS first_trade_ts, max(ts_exchange) AS last_trade_ts
FROM trades
GROUP BY venue, market_id;
