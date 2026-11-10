# Venue notes

Endpoints, authentication, rate limits, fee sources, terms and owner-country access for each
source (NFR-10, P-04, R-13). Read from the official documentation on **2026-11-09** unless a
row says otherwise. HTTP statuses are from plain unauthenticated `GET`s made from the owner's PC
on that date with `User-Agent: prediction-market-research/0.1 (+https://github.com/avmhdt/prediction-market-research)`.
Anything not confirmed in official docs is marked **UNVERIFIED**. The terms summaries are
readings for the owner's decisions (P-12, D-05), not legal advice.

Owner's country of residence: **KY (Cayman Islands)**, `OWNER_COUNTRY=KY`. No VPN, proxy or
IP rotation is ever used. A venue that refuses requests from the owner's network is dropped and
reported, never reached another way.

## Access summary

| Source | Base URL used | Auth for what this project reads | Rate limit (config) | Smoke 2026-11-09 | Owner-country access |
|---|---|---|---|---|---|
| Kalshi Trade API v2 | `https://external-api.kalshi.com/trade-api/v2` | none | 5 req/s | 200 on markets, order book, trades, historical cutoff | Public reads work. Account eligibility for KY: UNVERIFIED (no account is needed) |
| Polymarket Gamma | `https://gamma-api.polymarket.com` | none | 5 req/s | 200 | KY not on any blocked or close-only list |
| Polymarket CLOB | `https://clob.polymarket.com` | none (public market data) | 5 req/s | 200 | as above |
| Polymarket Data API | `https://data-api.polymarket.com/v2` | none | 5 req/s | 200 (v1 `/trades` returns 410) | as above |
| Polymarket on-chain | Dune or a subgraph (D-04) | Dune key; Dune's free tier has no API | n/a | not probed | n/a |
| Manifold | `https://api.manifold.markets/v0` | none | 5 req/s | 200 | no country rules for reads |
| Metaculus | `https://www.metaculus.com/api` | `Authorization: Token <token>` on every endpoint | 2 req/s | skipped (no token yet); 403 without a token | no country rules found |
| FRED DTB3 | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3` | none | 1 req/s | 200 | public |

### Current location: Brazil (re-checked 2026-11-10)

The owner's residence is KY, but the owner and this PC are in Brazil with no VPN, and the
network geolocates to BR (Polymarket's geoblock check: `blocked: true`, country BR). Everything
that depends on where traffic comes from is judged for BR. Sources were read on 2026-11-10;
this is a reading for the owner, not legal advice.

| Source | BR status for this project's use (public, read-only, no account) |
|---|---|
| Polymarket | **Unclear; collection paused pending the owner.** The ToS (effective 2026-08-11) applies restrictions to people "located in" a Restricted Jurisdiction, not only residents, and such users "are only permitted to use the Content Features" (informational features). It does not say whether unauthenticated API reads count. The docs geoblock page lists BR as close-only (no new positions); the help centre (<https://help.polymarket.com/en/articles/13364163-geographic-restrictions>, updated 2026-10-29) lists BR among countries "completely restricted from accessing Polymarket", while saying some restricted users "can view markets and data". Public reads return 200 from here. |
| Kalshi | Reading is not restricted by location. Brazil is not named in the restricted lists that could be read (an older Member Agreement copy; the Klear member agreement of 2026-06-22); their catch-all covers places where *trading* is illegal, which now likely includes Brazil. The Developer Agreement and Data Terms have no location clause; the storage restriction above is unchanged. The current Member Agreement could not be read (HTTP 429). |
| Manifold | No BR restriction; play money; the public API is allowed. Prize-drawing exclusions do not name Brazil and do not concern data access. |
| Metaculus | No BR-specific clause found (the terms page answered 403 to automated reads). |
| FRED, Dune | No BR restriction (FRED restricts only US-embargoed countries). |

Brazilian rules found on official sites:

- CMN Resolution 5.298 (24 Apr 2026, in force 4 May 2026) bans offering and trading, in Brazil,
  derivatives on sports, political, electoral, social, cultural and entertainment events,
  including offers in Brazil of contracts traded abroad
  (<https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Resolu%C3%A7%C3%A3o%20CMN&numero=5298>).
- Finance Ministry technical note SEI 2958/2026 (23 Apr 2026) names Kalshi and Polymarket and
  recommends blocking access by Brazilians; on 24 Apr 2026 ANATEL was asked to have ISPs block 28
  prediction-market companies (the official pages do not list them; only the press says Kalshi and
  Polymarket are included). No official source sanctions users or addresses reading public data.
- MP 1.394 (25 Sep 2026) bans offering and advertising fixed-odds betting, including content aimed
  at the Brazilian public that "facilitates access". This project publishes research aggregates,
  never betting links or promotion.

Rules for this project while in Brazil: no VPN, proxy or other way around a block; if reads from a
venue start failing, that venue stops and is reported; nothing is published that promotes or links
to betting for a Brazilian audience.

## Kalshi

Sources: <https://docs.kalshi.com/getting_started/api_environments>,
<https://docs.kalshi.com/getting_started/quick_start_market_data>,
<https://docs.kalshi.com/getting_started/historical_data>,
<https://docs.kalshi.com/getting_started/pagination>,
<https://docs.kalshi.com/getting_started/rate_limits>,
<https://docs.kalshi.com/getting_started/api_keys>, and `https://docs.kalshi.com/openapi.yaml`
(spec version 3.36.0).

- **Hosts.** `external-api.kalshi.com` is the documented production host;
  `api.elections.kalshi.com` still answers (both 200). Demo hosts are not used.
- **Endpoints with no auth.** `GET /series`, `/series/{s}`, `/events` (limit ≤ 200),
  `/markets` and `/markets/{ticker}` (limit ≤ 1000; filters for status, close, settled and
  updated times), `/markets/{ticker}/orderbook` (`depth` 0–100; yes bids and no bids only),
  `/markets/orderbooks` (≤ 100 tickers), `/markets/trades` (limit ≤ 1000, `min_ts`, `max_ts`),
  `/series/{s}/markets/{t}/candlesticks` (`period_interval` 1, 60 or 1440 minutes),
  `/markets/candlesticks` (batch), and the historical set `/historical/cutoff`,
  `/historical/markets`, `/historical/markets/{t}/candlesticks`, `/historical/trades`.
  Only user-scoped historical fills, orders and positions need a key, and they are never used.
  The WebSocket needs a key even for public channels; this project polls REST instead.
- **Live/historical split.** Settled markets and their candlesticks older than
  `market_settled_ts`, and trades older than `trades_created_ts`, exist only under
  `/historical/*`. Events and series stay live. On 2026-11-09 both cutoffs were
  2026-09-10T00:00:00Z.
- **Pagination.** Cursor: pass the response's `cursor`; stop when it is empty.
- **Rate limits.** Token bucket: 10 tokens per request; Basic tier 200 read tokens/s
  (about 20 req/s) with a 3 s burst. 429 on excess, without `Retry-After`. Limits for
  unauthenticated traffic are not documented (UNVERIFIED), so the config stays at 5 req/s.
- **Signing (optional, FR-02).** `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE` = base64 signature of `timestamp + METHOD + path`, where the path
  starts at `/trade-api/v2`, has no query string and no host. Ed25519 (recommended) or RSA-PSS
  SHA-256 with MGF1-SHA-256 and salt length equal to the digest length. Implemented in
  `src/pmr/http/kalshi_auth.py`; not used, because every endpoint above is public.
- **Fees.** Fee page <https://kalshi.com/fee-schedule> ("No upcoming fee changes scheduled");
  schedule PDF <https://kalshi.com/docs/kalshi-fee-schedule.pdf>, titled
  "Fee Schedule for July 2026 - 7.7.26 Update"; rounding rules
  <https://docs.kalshi.com/getting_started/fee_rounding>; API `GET /series/fee_changes`.
  Coefficients are transcribed from these documents in M3 only.
- **Account eligibility.** Trading from outside the US is subject to the Member Agreement
  (<https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states>).
  Whether KY is restricted could not be read first-hand (the agreement PDF could not be
  extracted, and kalshi.com answered 429 to automated reads): **UNVERIFIED**. No account is
  needed for this project.
- **Terms on data use: needs the owner's decision before M2.**
  - Kalshi Developer Agreement v1.1 (<https://assets.kalshi.com/Kalshi-Developer-Agreement.pdf>,
    linked from <https://kalshi.com/developer-agreement>): section 1 says using the API accepts
    the agreement. Section 3 limits API use to "facilitating a members own trading on the
    Exchange". Section 3.1 prohibits collecting, caching, aggregating or storing API data
    except for that purpose, and sharing it with third parties without written authorization.
    Section 3.5 prohibits access for benchmarking or competitive purposes.
  - Kalshi Data Terms of Use
    (<https://kalshi-public-docs.s3.amazonaws.com/kalshi-data-terms-of-service.pdf>, linked in
    the kalshi.com footer): personal, non-commercial use only. Without written authorization it
    prohibits copying, downloading, compiling databases, text-and-data mining, redistributing,
    publicly displaying, building products or indexes, and any ML/AI use.
  - Reading: building a persistent Kalshi archive for research (M2) and publishing derived
    Kalshi aggregates (D-05) both appear to need Kalshi's written permission. The spec assumed
    only "no raw redistribution".

## Polymarket

Sources: <https://docs.polymarket.com/api-reference/predictions/overview>,
<https://docs.polymarket.com/market-data/discover-markets>,
<https://docs.polymarket.com/api-reference/data-api/overview>,
<https://docs.polymarket.com/migrate/data-api-v1-to-v2>,
<https://docs.polymarket.com/api-reference/rate-limits>,
<https://docs.polymarket.com/api-reference/geoblock>, <https://docs.polymarket.com/trading/fees>,
<https://docs.polymarket.com/changelog/predictions>, <https://polymarket.com/tos>
(effective 2026-08-11).

- **Gamma (no auth).** Keyset pagination is recommended: `GET /events/keyset`,
  `GET /markets/keyset`, `limit` 1–100, `after_cursor` from `next_cursor`; `offset` there
  returns 422. Legacy `GET /events` and `/markets` take `limit`/`offset` and are to be
  deprecated; their maximum offset is UNVERIFIED. Markets: `closed=true` for closed only;
  omitted or `false` is open only; `closed=any` needs an identifier filter.
- **CLOB (no auth for market data).** `GET /book?token_id=<token>`; `GET /prices-history`
  takes the outcome **token id** as `market`, with `startTs`, `endTs`, `interval`
  (max, all, 1m, 1w, 1d, 6h, 1h) and `fidelity` in minutes.
- **Data API: v1 retired on 2026-11-02.** v1 `/trades` returns
  `410 {"error":"Data API v1 has been retired"}`. v2 lives at `/v2`, needs no key, uses
  cursor-only pagination (`pagination.next_cursor`; `offset` returns 400, `limit` applies to
  the first page only) and filters markets with `condition` (≤ 20 condition ids).
  `GET /v2/trades`: `limit` ≤ 1000, `taker_only` (default true), `condition`, `event_id`,
  `side`. Queries by `condition` or `event_id` serve a fixed **three-year window**; the bare
  global feed serves only the current and previous month. `GET /v2/prices-history`
  (by `token_id`) keeps raw data ≥ 7 days, 5-minute ≥ 60 days, 30-minute ≥ 90 days, and
  3-hour and 12-hour bars permanently.
- **Rate limits** (Cloudflare, per IP; excess is throttled rather than rejected). Gamma
  `/markets` 300 per 10 s, `/events` 500 per 10 s; Data API `/v2/trades` 300 per 10 s,
  `/v2/prices-history` 200 per 10 s; CLOB `/book` 1,500 per 10 s, `/prices-history`
  1,000 per 10 s. The config stays at 5 req/s per host.
- **Geoblocking.** Check endpoint `GET https://polymarket.com/api/geoblock` returns
  `{blocked, ip, country, region}` and describes eligibility to place orders. Fully blocked:
  IR, SY, CU, KP and three Ukrainian regions. Close-only (frontend and API) includes US, GB,
  FR, DE, IT, BR and others; close-only on the frontend only: IE, JP, NL, KR, MT (sports).
  **KY is on no list.** Public reads were not geoblocked from this PC (see above).
- **Fees.** Taker only; makers are never charged.
  `fee = C × feeRate × p × (1 − p)`, rounded to 5 decimal places (minimum 0.00001 USDC), with
  per-category rates and a per-market `feeSchedule` object. Rates are transcribed into
  `config/fees.yaml` in M3, with earlier schedules from archived copies.
- **Cached responses.** Gamma list responses can come from Cloudflare's cache with an older
  `Date` header (the M1 smoke saw a +27 s offset on Gamma while every other host was within
  about 1 s). Clock-offset checks (DQ-01) use the CLOB and Data API hosts, not Gamma.
- **Subgraph / on-chain.** The current docs list no subgraph endpoint. On-chain pages point to
  Goldsky, Dune and Allium (<https://docs.polymarket.com/resources/blockchain-data>). Dune's
  curated tables include `polymarket_polygon.market_trades`.
- **Terms.** Personal, limited, revocable licence (section 5.1). Section 4.2: "Capital Market
  Clients" (professional capital-markets entities) and market-data distributors need a written
  agreement, and nobody may sell, resell, sublicense, redistribute or commercially exploit the
  data to them. Scraping with robots and circumventing access controls, including by VPN, are
  prohibited. Residents of restricted jurisdictions may use only informational features.

## Manifold

Source: <https://docs.manifold.markets/api>, <https://docs.manifold.markets/data>.

- Base `https://api.manifold.markets/v0` (the API is labelled alpha; the old
  `manifold.markets/api` host is being retired). Reads need no auth.
- `GET /v0/markets`: `limit` ≤ 1000, `sort`, `order`, `before` (market-id cursor).
  `GET /v0/search-markets`: `filter=resolved`, `contractType=BINARY`, `limit` ≤ 1000,
  `offset` ≤ 1000 (deeper: `sort=newest` with `beforeTime`). `GET /v0/bets`: `contractId`,
  `limit` ≤ 1000, `before`/`after` bet-id cursors, `beforeTime`/`afterTime` in ms.
- Rate limit: 500 requests per minute per IP; using several IPs to evade it is not allowed.
- Fees: play money (mana). Any API fee in mana is documented in M3.
- Terms: academic, personal and non-commercial use permitted; commercial AI/ML training needs a
  data licence; scraping outside the API is prohibited. Free bulk dumps (last updated
  2024-07-06) are for personal, non-commercial use.

## Metaculus

Source: the OpenAPI description linked from <https://www.metaculus.com/api/> ("Metaculus API"
2.0.0), <https://www.metaculus.com/terms-of-use/> (last modified 2026-09-21).

- **Base is `https://www.metaculus.com/api`**, not `/api2` as the spec suggested: `/api2` is
  not in the current OpenAPI description (answers 403 here). Config updated (A-10).
- **Every endpoint needs `Authorization: Token <token>`** and returns 403 without one
  ("The API is only available to authenticated users."). Listing: `GET /api/posts/` with
  `statuses`, `forecast_type`, time filters, `with_cp`, `include_cp_history`,
  `include_descriptions`, `limit`, `offset`.
- **Community-prediction history needs an upgraded tier.** Default ("Restricted") accounts
  always get `aggregations.*.history = null`; current CP is only on about 50 questions. The
  "Bot Benchmarking" tier adds about 500 questions. Full history needs the "Unrestricted" tier,
  granted on request through the Metaculus Data Needs Form. Closed-question text and resolution
  are null unless the account forecast on the question. FR-07 and AC-H-06 depend on this.
- Rate limits: throttling is mentioned without numbers (UNVERIFIED); config stays at 2 req/s.
- Terms: personal, non-commercial, revocable licence; automated extraction only through the
  API; no copying, republishing or distribution without written permission; no AI/ML training;
  commercial use needs a written agreement.

## FRED DTB3

Source: <https://fred.stlouisfed.org/series/DTB3>.

- `fredgraph.csv?id=DTB3` needs no key; columns `observation_date,DTB3`; percent, daily,
  1954-01-04 onward; missing values are empty fields. The endpoint answers 404 to
  `Accept: application/json`, so the client sends a CSV `Accept` header for it.
- Board of Governors of the Federal Reserve System (US), H.15 release; "Public Domain:
  Citation Requested". Cite as: Board of Governors of the Federal Reserve System (US),
  3-Month Treasury Bill Secondary Market Rate, Discount Basis [DTB3], retrieved from FRED,
  Federal Reserve Bank of St. Louis; https://fred.stlouisfed.org/series/DTB3.

## Dune (optional, D-04)

Sources: <https://docs.dune.com/resources/credits-billing/how-credits-work>,
<https://docs.dune.com/api-reference/overview/billing>,
<https://docs.dune.com/api-reference/overview/rate-limits>,
<https://docs.dune.com/data-catalog/curated/prediction-markets/all-tables-overview>.

- **The free tier has no API access and no credits.** New accounts get a 14-day trial with
  2,500 credits. Paid plans start at USD 75 a month, which D-02 does not approve.
- Endpoints: `POST /api/v1/query/{id}/execute` (the only POST the guard allows),
  `GET /api/v1/execution/{id}/results`, `GET /api/v1/query/{id}/results`; key in the
  `X-Dune-Api-Key` header.
- Consequence for D-04 and DQ-13: the default "Dune free tier for the reconciliation sample"
  is not available at USD 0. The M2 plan for on-chain fills is decided and recorded at M2.

## Publication (P-12, D-05)

To be completed before M6 with the owner's publish-or-exclude choice per venue. Current
reading of the terms above: Kalshi and Metaculus require written permission for any
redistribution; Polymarket forbids redistribution to capital-markets clients and data
distributors; Manifold allows non-commercial use. Only derived aggregates are ever
candidates for publication (D-05).
