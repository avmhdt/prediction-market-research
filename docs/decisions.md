# Decisions

Agent choices on ambiguities, plus the owner's decisions that apply to this project. The owner's
file `DECISIONS.md` (outside this repo) overrides the build spec, and the spec overrides
`README.md`.

## Owner decisions for this project

Copied at M1 (2026-11-09) from the owner's `DECISIONS.md`, recorded 2026-10-31 with later rulings.

| Item | Decision |
|---|---|
| Country (P-04) | Cayman Islands, `OWNER_COUNTRY=KY`. Never a VPN or proxy. A venue that refuses KY gets public read-only data only, or is dropped and reported. |
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
