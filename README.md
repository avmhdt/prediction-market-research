# Prediction market research

> Measure how well Kalshi and Polymarket prices forecast outcomes, and where pricing errors survive fees.

**Status:** planning

| | |
|---|---|
| Track | Quant Researcher |
| Estimated effort | 4–6 weeks |
| Stack | Python, DuckDB |
| Data cost | Free |
| Strongest for | Event-contract desks; a fresh talking point anywhere |

## Requirements

- [ ] Collectors for Kalshi and Polymarket metadata, order books, trades and resolutions, plus Manifold as a play-money contrast.
- [ ] A unified schema and a cross-venue contract matcher (manual map plus text similarity) that flags differences in resolution rules.
- [ ] Calibration: reliability diagrams and Brier scores by category, price bucket and time to expiry.
- [ ] Favorite–longshot bias regressions with bootstrap confidence intervals.
- [ ] Fee models for each venue. Check current schedules; Kalshi’s fee scales with P × (1 − P).
- [ ] A cross-venue arbitrage scanner and a simple market-making simulation with inventory limits.

## Data

Raw and licensed data live in `data/`, which is git-ignored. Only download scripts and small synthetic fixtures are committed.

| Dataset | Cost | What it gives you |
|---|---|---|
| [Kalshi API](https://docs.kalshi.com) | Free | Markets, order books, trades, candlesticks and settlements. Needs an account. |
| [Polymarket Gamma and CLOB APIs](https://docs.polymarket.com) | Free | Market metadata, order books and price history. |
| [Polymarket on-chain data (Dune)](https://dune.com) | Free tier | Every fill is on-chain, so full trade history is queryable. |
| [Manifold API](https://docs.manifold.markets/api) | Free | Play-money markets. A useful calibration baseline. |
| [Metaculus](https://www.metaculus.com) | Free | Aggregated forecasts on overlapping questions. |

## Success criteria

### Works (correct and reproducible)

- [ ] Calibration computed on thousands of resolved markets, with confidence intervals.
- [ ] The arbitrage scanner accounts for fees, capital lockup and resolution-rule mismatches.

### Impresses hirers

- [ ] A quantified favorite–longshot bias by category, and whether it survives fees.
- [ ] A catalogue of ‘same event, different resolution’ cases and what they would have cost.
- [ ] Weeks of live paper-trading logs compared with your simulator’s predictions.
- [ ] A clean public dataset that other people actually use.

## Questions this project must answer

- Why might a 95¢ contract be fairly priced even if it resolves YES 97% of the time?
- How do you price capital that is locked up until resolution?
- What is your edge against informed traders?
