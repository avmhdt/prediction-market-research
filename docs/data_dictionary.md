# Data dictionary

Every warehouse table and column (NFR-10). The warehouse is one DuckDB file,
`$DATA_ROOT/warehouse.duckdb`, rebuilt by `pmr warehouse build` from the interim Parquet that
`pmr normalize` writes from the raw archive. Schema version: table `meta_schema`
(`src/pmr/warehouse/ddl.sql`, then `migrations/NNN_*.sql`).

Conventions: timestamps are `BIGINT` nanoseconds since the Unix epoch, UTC. Prices are
YES-orientation probabilities in [0, 1]: USD per USD 1 payoff (USDC on Polymarket), mana per
mana 1 on Manifold, a probability forecast on Metaculus. `venue` is one of `kalshi`,
`polymarket`, `manifold`, `metaculus`. `symbol` is the venue's tradable id: the Kalshi market
ticker, the Polymarket YES token id (the first outcome's token), the Manifold market id, the
Metaculus question id.

## markets

Instrument metadata with Type-2 history (DQ-03): one row per version, primary key
`(venue, symbol, valid_from)`.

| Column | Type | Meaning |
|---|---|---|
| venue | VARCHAR | Venue. |
| symbol | VARCHAR | Venue-native tradable id (see above). |
| market_id | VARCHAR | Kalshi ticker; Polymarket condition id; Manifold id; Metaculus question id. |
| event_id | VARCHAR | Grouping and bootstrap cluster key (DQ-06): Kalshi event ticker, Polymarket event id, Manifold market id (no events), Metaculus post id. |
| series_id | VARCHAR | Kalshi series ticker (from the event); null elsewhere. |
| title, subtitle, outcome_label | VARCHAR | Question text; Kalshi subtitle or Polymarket group item; label of the YES outcome. |
| rules_text, rules_secondary | VARCHAR | Resolution rules verbatim (Kalshi primary and secondary rules; Polymarket description; Manifold description; Metaculus resolution criteria and fine print). |
| rules_hash | VARCHAR | SHA-256 (16 hex characters) of both rules texts. |
| resolution_source | VARCHAR | Named source or URL, when the venue gives one. |
| category_raw | VARCHAR | Venue labels joined with `\|` (Kalshi series category; Polymarket tags; Manifold groups; Metaculus categories). |
| category | VARCHAR | Project taxonomy (D-08, `config/categories.yaml`): politics, economics, finance, crypto, sports, weather-climate, science-tech, culture, other. |
| open_ts, close_ts | BIGINT | Trading opened; trading closed (Polymarket: actual `closedTime`, else the scheduled end). |
| expected_resolution_ts | BIGINT | Scheduled end or expiration. |
| resolved_ts | BIGINT | When the result was set (when the venue reports it). |
| rules_tz | VARCHAR | Time zone stated in the rules (filled by the M3 rule extractor). |
| status | VARCHAR | `open`, `closed`, `resolved`, `voided`, `disputed` (closed with an open dispute). |
| result | VARCHAR | `YES`, `NO`, `VOID` (cancelled or annulled), `SCALAR` (partial payout, including Polymarket 50-50 and Manifold MKT). Null while unresolved. |
| settlement_value | DOUBLE | Payoff per YES contract in [0, 1]. |
| tick_size, lot_size | DOUBLE | Minimum price increment and order size, when given. |
| contract_type | VARCHAR | Always `binary_event`. |
| currency | VARCHAR | `USD` (Kalshi), `USDC` (Polymarket), `MANA` (Manifold), null (Metaculus). |
| is_play_money | BOOLEAN | True for Manifold (DQ-10). |
| is_forecast | BOOLEAN | True for Metaculus (DQ-10). |
| fee_schedule_id | VARCHAR | Link to the fee schedule (filled in M3). |
| no_symbol | VARCHAR | Polymarket NO token id (parity checks, DQ-04). |
| disputed | BOOLEAN | Polymarket market whose UMA status history contains a dispute (DQ-05; analysed with and without). |
| n_traders | BIGINT | Manifold unique bettors; Metaculus forecasters. |
| volume | DOUBLE | Lifetime volume as the venue reports it (venue units). |
| rules_changed | BOOLEAN | This version's rules differ from the first version's (DQ-03). |
| valid_from, valid_to | BIGINT | Version validity; `valid_to` null for the current version. |
| ingested_at | BIGINT | Receipt time of the raw page this version came from. |

## events

Kalshi event and series lookups (event to series to category). A row with null `event_id`
is a series.

| Column | Type | Meaning |
|---|---|---|
| venue | VARCHAR | `kalshi`. |
| event_id | VARCHAR | Event ticker; null for a series row. |
| series_id | VARCHAR | Series ticker. |
| title | VARCHAR | Event or series title. |
| category_raw | VARCHAR | Kalshi category. |
| ingested_at | BIGINT | Receipt time. |

## trades

Unique on `(venue, trade_id)` (DQ-08). Canonical fields plus additions.

| Column | Type | Meaning |
|---|---|---|
| ts_exchange | BIGINT | Venue trade time. |
| ts_recv | BIGINT | Local receipt time of the page (DQ-01). |
| venue, symbol | VARCHAR | Keys; Polymarket NO prints carry the market's YES symbol. |
| trade_id | VARCHAR | Venue trade id; Polymarket: hash of transaction hash, token, side, size, price, time and wallet; Manifold: bet id. |
| price | DOUBLE | YES price; NO-side prints converted as 1 - p. |
| qty | DOUBLE | Contracts or shares. |
| side | TINYINT | +1 buyer of YES initiated, -1 seller of YES initiated, 0 unknown. |
| market_id | VARCHAR | As in `markets`. |
| source | VARCHAR | `api` (live), `historical` (Kalshi historical endpoints), `onchain`. The live record wins a duplicate. |
| notional | DOUBLE | Venue currency: price paid times quantity (Manifold: mana amount). |

## price_history

Primary key `(venue, symbol, ts, interval_s)`.

| Column | Type | Meaning |
|---|---|---|
| venue, symbol | VARCHAR | Keys. |
| ts | BIGINT | Bar end (Kalshi candles), point time (Polymarket), bet time (Manifold), start of the aggregate interval (Metaculus). |
| interval_s | INTEGER | 3600 (Kalshi hourly; Polymarket hourly), 43200 (Polymarket 12-hour), 0 (ticks: Manifold bets, Metaculus aggregates). |
| open, high, low, close | DOUBLE | YES prices; single-price sources fill `close` only. |
| yes_bid_close, yes_ask_close | DOUBLE | Kalshi candle bid and ask at bar end. |
| volume, open_interest | DOUBLE | Kalshi candle volume and open interest. |
| n_forecasters | INTEGER | Metaculus forecaster count. |

## book_l2

Polled full snapshots (`is_snapshot = true`).

| Column | Type | Meaning |
|---|---|---|
| ts_exchange | BIGINT | Venue book time (Polymarket); null when the venue gives none (Kalshi). |
| ts_recv | BIGINT | Local receipt time. |
| venue, symbol | VARCHAR | Keys. Polymarket NO-token books are stored under the NO token id (DQ-04). |
| seq | BIGINT | Snapshot counter per symbol, in receipt order. |
| side | TINYINT | +1 YES bid, -1 YES ask (Kalshi NO bid at q stored as a YES ask at 1 - q). |
| price, qty | DOUBLE | Level price (YES) and size. |
| is_snapshot | BOOLEAN | Always true (polled snapshots). |

## risk_free

| Column | Type | Meaning |
|---|---|---|
| date | DATE | Observation date. |
| series | VARCHAR | `DTB3`: 3-month Treasury bill secondary-market rate, discount basis (FRED). |
| rate_pct | DOUBLE | Percent per year; blank FRED days are omitted. |

## Views

| View | Meaning |
|---|---|
| markets_current | Current version of each market (`valid_to IS NULL`). |
| bbo | Best bid and ask (price and size) per book snapshot. |
| resolved_binary | Current markets with a final YES/NO result, with `y` = 1 for YES and the play-money, forecast and dispute flags. |
| market_liquidity | Trade count, notional and first and last trade time per market. |

`calib_snapshots` arrives in M5 with the DQ-02 snapshot rule (docs/decisions.md A-20).
Later milestones add `fee_schedules`, `contract_pairs`, `rule_diffs`, `arb_opportunities`,
`mismatch_catalogue` and the `paper_*` tables through migrations.
