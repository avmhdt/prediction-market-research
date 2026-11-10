# Decisions

Agent choices on ambiguities, plus the owner's decisions that apply to this project. The owner's
file `DECISIONS.md` (outside this repo) overrides the build spec, and the spec overrides
`README.md`.

## Owner decisions for this project

Copied at M1 (2026-11-09) from the owner's `DECISIONS.md`, recorded 2026-10-31 with later rulings.

| Item | Decision |
|---|---|
| Country (P-04) | Cayman Islands, `OWNER_COUNTRY=KY`. Never a VPN or proxy. A venue that refuses KY gets public read-only data only, or is dropped and reported. |
| Current location (2026-11-10) | The owner's residence is KY, but the owner and this PC are currently in Brazil, with no VPN, and the PC's network location is BR. Anything that depends on where traffic comes from uses BR. Public read-only data is fine; never place orders and never get around a geoblock. See docs/venue_notes.md for the BR check. |
| Kalshi storage (2026-11-10) | Not allowed until the owner rules on Kalshi's Developer Agreement and Data Terms. Enforced in code: `venues.kalshi.storage_allowed: false` makes every Kalshi ingest refuse before any request. |
| Metaculus (2026-11-10) | No Metaculus endpoint that needs the token is used until `METACULUS_API_TOKEN` is in `.env`. |
| Paper-trading window (2026-11-10) | The owner keeps Windows Update and reboots off through about 2026-12-15, so the 28-day run must end by then; the planned start and end go to the owner before it starts. |
| Spec defaults | Every decision in spec section 3 with a default stands as written (D-01 to D-12), apart from what is listed here. |
| Money (D-02) | USD 0. Free tiers only (Dune free tier, public endpoints). |
| Always-on host (D-06) | Owner's PC under WSL2 Ubuntu 22.04.5 with systemd. At least 4 weeks of paper trading; dates are given to the owner before the run starts. |
| Multi-week runs | Must run under cron or systemd and end before the Windows Update pause expires on 2026-12-06, unless the owner says it was extended. |
| Venue publication (P-12, D-05) | Still open. Before M6, `docs/venue_notes.md` is drafted and the owner chooses publish or exclude per venue. Only derived aggregates are ever published. |
| Metaculus token (P-06) | The owner adds `METACULUS_API_TOKEN` to `.env` before M2. Keys are never asked for in chat, printed, logged or put in URLs. |
| Kalshi and Dune keys (P-05, P-07) | Optional; public endpoints are the default path. |
| Merging | One milestone per PR, merged with `gh pr merge <n> --auto --squash` as `avmhdt` once CI is green. Branch protection on `main` requires CI. |
| Shared PC | Downloads at most 10 MB/s; heavy runs at most 8 threads at `nice 10`; stop if free memory drops below 30 GB; keep `D:` above 150 GB free; never restart WSL, reboot or edit `.wslconfig`; data lives in WSL ext4, never on `C:` and never in git; publish aggregates only. |
| Clock | Windows syncs with time.cloudflare.com every 1,024 s (set by the owner 2026-11-09). Ubuntu's chronyd must not steer the clock. |
| Attribution | No AI or assistant attribution anywhere in the repo, commits or PRs. |

## Machine (reference hardware for NFR-03)

Recorded 2026-11-09: Intel Core i9-14900K (24 cores, 32 threads), 128 GB RAM, Windows 11 Pro.
`D:` is a 2 TB SATA HDD with 583 GB free at M1; the WSL2 distro lives on it. Shared with
other projects' live runs, so heavy jobs use at most 8 threads.

## Agent decisions

| ID | Date | Decision | Why |
|---|---|---|---|
| A-01 | 2026-11-09 | Polymarket Data API base is `https://data-api.polymarket.com/v2`. | v1 now answers `410 Gone` with "Data API v1 has been retired" and points to `/v2/`. v2 `/trades` returns `{"data": [...]}` with snake_case fields (`transaction_hash`, `token_id`, `condition_id`). |
| A-02 | 2026-11-09 | `VenueClient.get` returns `dict | list` (the parsed JSON), not only `dict` as sketched in spec section 10. | Polymarket Gamma and Manifold return top-level JSON arrays. `VenueClient.fetch` also returns the response metadata the raw archive needs (DQ-12). |
| A-03 | 2026-11-09 | Retries are a small hand-written loop instead of `tenacity`, so `tenacity` is not a dependency. | The loop must penalise the host limiter on 429, honour `Retry-After`, use an injectable sleep for tests, and redact secrets in its logs; written directly it is shorter and fully tested. Behaviour matches FR-12: at most 6 attempts, full-jitter exponential backoff (base 1 s, cap 60 s), retry on 429, 5xx, timeouts and transport errors. |
| A-04 | 2026-11-09 | The guard treats `/order`, `/orders`, `/portfolio` (and cancel, auth, wallet, deposit, withdraw, transfer) as whole path segments, plus any segment starting `order-`/`order_`/`portfolio-`/`portfolio_`. | AC-W-10 says "paths containing /portfolio or /order". A plain substring test would also block Kalshi's public `/markets/{ticker}/orderbook`, which the arb scanner and recorder need. Both cases are unit-tested. The guard also refuses hosts that are not on its allowlist and plain `http`. |
| A-05 | 2026-11-09 | Dependencies are added in the milestone that first uses them. M1 adds the HTTP, CLI, config and logging stack; Polars, DuckDB, PyArrow, NumPy, SciPy, statsmodels, rapidfuzz, matplotlib and zstandard arrive in M2 to M5; `sentence-transformers` (CPU torch) in M3. | Keeps the M1 lockfile and CI small; every package is still from the spec section 4 table. |
| A-06 | 2026-11-09 | `${NAME}` placeholders in YAML configs are filled after parsing, in string values only, from `.env` settings first and then the environment; `${NAME:-default}` is supported; secrets can never be interpolated. | The spec writes `base_url: ${KALSHI_API_BASE}`; filling after parsing means comments in YAML files are never touched. |
| A-07 | 2026-11-09 | On native Windows a `DATA_ROOT` that starts with `~` is ignored (falls back to `./data` in the repo on `D:`), and any data directory or warehouse path on `C:` is refused. | One `.env` serves both WSL and Windows runs. In WSL `~` is the ext4 home, as the spec intends; on Windows `~` would be `C:`, which the shared-PC rules forbid. |
| A-08 | 2026-11-09 | `pmr smoke` probes one read per endpoint family: Kalshi markets, order book, trades, historical cutoff; Polymarket Gamma markets, CLOB book, Data API trades; Manifold markets; Metaculus questions (optional, needs the token); FRED DTB3. It also reads Polymarket's public geoblock endpoint as an optional probe and keeps only the `blocked` flag and country, never the IP. | Covers every source in spec section 7a that is reachable without a key. Dune and the subgraph are skipped until a key or endpoint is configured (both optional). |
| A-09 | 2026-11-09 | CI pins `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.3`, as in the owner's other repos, and runs gitleaks with `gitleaks/gitleaks-action@v2`. | Same toolchain across the portfolio. |
| A-10 | 2026-11-09 | Metaculus base URL is `https://www.metaculus.com/api` (smoke probes `GET /api/posts/`). | The current official OpenAPI description has no `/api2`; see docs/venue_notes.md. |
| A-11 | 2026-11-09 | Rate limits stay at the spec's conservative 5 req/s (Kalshi, Polymarket, Manifold) and 2 req/s (Metaculus). | Documented limits are higher (Kalshi Basic about 20 req/s, Polymarket hundreds per 10 s, Manifold 500/min), but Kalshi does not document limits for unauthenticated traffic and Metaculus gives no numbers. |
| A-12 | 2026-11-10 | Polymarket trades come from Data API v2 `GET /v2/trades?condition=<id>` with the default `taker_only=true` (one row per taker fill). v2 has no log index, so `trade_id` is a hash of transaction hash, token, side, size, price, time and wallet. NO-outcome prints are stored as YES prices (`1 - p`) with the side flipped, and get the market's YES symbol in the warehouse. | Taker rows avoid counting both legs of a fill; the hash makes re-ingests idempotent (DQ-08). |
| A-13 | 2026-11-10 | Polymarket price history: CLOB `/prices-history` over the whole life at 12-hour fidelity (`interval=all&fidelity=720`), plus hourly bars for the 32 days before close in windows of at most 15 days. | For closed markets the CLOB returns nothing for `interval=max`, rejects `startTs`/`endTs` windows over 15 days, and Data API v2 keeps only 12-hour bars for old markets (measured 2026-11-10). Hourly bars cover the 1 h to 30 d horizons (DQ-02); point-in-time prices for calibration also come from trades. |
| A-14 | 2026-11-10 | Gamma markets are listed with `closed=true`, `include_tag=true` and `end_date_min` set 180 days before the study window; the window itself is applied to the actual close time after normalization. | `end_date` is the scheduled end, which can precede the actual close (`closedTime`); filtering on it alone could drop markets that closed inside the window (DQ-09). |
| A-15 | 2026-11-10 | Polymarket trades are collected only for markets with lifetime volume of at least USD 100. | Such markets can never pass the DQ-07 filter (USD 100 notional). Price history is still collected for every closed market, so the unfiltered sensitivity run keeps them. |
| A-16 | 2026-11-10 | Manifold: the lite list gives every market; full records and bets are fetched only for resolved or voided binary markets that closed in the window with at least 10 unique bettors (D-01). `MKT` resolutions are SCALAR at `resolutionProbability`; `CANCEL` is VOID. Bets give trades (price = amount / shares, NO bets mirrored) and a tick price history from `probAfter`. Redemptions, cancelled bets and unfilled limit orders are skipped. | Keeps requests to markets the analysis can use; a lite record is replaced by the full one in the warehouse. |
| A-17 | 2026-11-10 | Metaculus aggregate history is stamped at each interval's `start_time`. | The aggregate is public from that moment, so no snapshot can see it early (DQ-02); stamping at the end time made the open-ended last interval collide with the previous one. |
| A-18 | 2026-11-10 | Interim files are named after their raw part, and DuckDB reads them with `hive_partitioning = false`. | Naming makes re-normalizing a part rewrite the same file (idempotent). The `date=` folder level (the raw part's day) otherwise overrides `risk_free.date`, which collapsed the FRED series to one row in the first build. |
| A-19 | 2026-11-10 | Markets keep Type-2 history: consecutive identical versions collapse, a lite record is dropped when a full one exists, and `rules_changed` flags a version whose rules differ from the first (DQ-03). Out-of-range timestamps (a Manifold close time in the year 2400) become null. | Needed for DQ-03; the year-2400 value overflowed int64 nanoseconds. |
| A-20 | 2026-11-10 | The `calib_snapshots` view (FR-10) is built in M5 together with the DQ-02 snapshot rule and its leakage test. | Spec section 16 requires the headline splits and filters to be written in docs/methodology.md before the first real-data calibration run; the view belongs with that work. M2 ships `markets_current`, `bbo`, `resolved_binary` and `market_liquidity`. |
| A-21 | 2026-11-10 | Polymarket on-chain fills (FR-05, DQ-13): Dune's free tier has no API access, and Polymarket's docs list no public subgraph. The collector queries a graph-node subgraph with GraphQL over GET and runs only when `POLYMARKET_SUBGRAPH_URL` is set; `reconcile()` implements the DQ-13 test. | Keeps the guard's GET-only rule. The reconciliation run waits on a free endpoint or an owner decision (docs/venue_notes.md). |
| A-22 | 2026-11-10 | Tests write only under `.pytest_tmp/` in the repo (git-ignored, on D:) and fail on any real network connection unless marked `network`. | The shared-PC rules forbid data on C:; an early test run reached the live Gamma API through a CLI test, and the guard makes that impossible. |
