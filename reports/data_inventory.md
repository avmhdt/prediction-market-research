# Data inventory

Generated 2026-11-17 20:47 UTC by `pmr inventory` from the warehouse. Study window (D-01): markets whose close time falls in 2023-01-01 to latest. Counts only.

## Eligible resolved real-money markets

Final YES/NO result (DQ-05), real money, at least 10 trades and USD 100 notional at or before the close (DQ-07). M2 needs 3,000 in total; AC-W-01 later needs 1,000 per real-money venue.

| venue | eligible | ≥ 1,000? |
|---|---|---|
| kalshi | 0 | no |
| polymarket | 0 | no |

Total eligible real-money markets: **0** (below the M2 threshold of 3,000).

Play-money and forecast venues (never pooled with real money, DQ-10): none.

## Markets by venue and status (DQ-09: no survivorship filter)

| venue | status | markets |
|---|---|---|
| manifold | closed | 3,574 |
| manifold | open | 25,325 |
| manifold | resolved | 94,612 |
| manifold | voided | 7,885 |

## Markets by venue and close year

| venue | close year | markets |
|---|---|---|
| manifold | 2023 | 39,977 |
| manifold | 2024 | 31,985 |
| manifold | 2025 | 18,631 |
| manifold | 2026 | 18,418 |
| manifold | 2027 | 4,600 |
| manifold | 2028 | 2,891 |
| manifold | 2029 | 3,066 |
| manifold | 2030 | 3,325 |
| manifold | 2031 | 1,261 |
| manifold | 2032 | 357 |
| manifold | 2033 | 399 |
| manifold | 2034 | 449 |
| manifold | 2035 | 759 |
| manifold | 2036 | 375 |
| manifold | 2037 | 77 |
| manifold | 2038 | 70 |
| manifold | 2039 | 243 |
| manifold | 2040 | 834 |
| manifold | 2041 | 280 |
| manifold | 2042 | 47 |
| manifold | 2043 | 61 |
| manifold | 2044 | 59 |
| manifold | 2045 | 140 |
| manifold | 2046 | 77 |
| manifold | 2047 | 26 |
| manifold | 2048 | 26 |
| manifold | 2049 | 172 |
| manifold | 2050 | 699 |
| manifold | 2051 | 209 |
| manifold | 2052 | 18 |
| manifold | 2053 | 26 |
| manifold | 2054 | 15 |
| manifold | 2055 | 27 |
| manifold | 2056 | 19 |
| manifold | 2057 | 4 |
| manifold | 2058 | 6 |
| manifold | 2059 | 32 |
| manifold | 2060 | 134 |
| manifold | 2061 | 84 |
| manifold | 2062 | 10 |
| manifold | 2063 | 22 |
| manifold | 2064 | 14 |
| manifold | 2065 | 12 |
| manifold | 2066 | 16 |
| manifold | 2067 | 3 |
| manifold | 2068 | 2 |
| manifold | 2069 | 35 |
| manifold | 2070 | 46 |
| manifold | 2071 | 24 |
| manifold | 2072 | 8 |
| manifold | 2073 | 8 |
| manifold | 2074 | 12 |
| manifold | 2075 | 47 |
| manifold | 2076 | 14 |
| manifold | 2077 | 12 |
| manifold | 2078 | 2 |
| manifold | 2079 | 7 |
| manifold | 2080 | 52 |
| manifold | 2081 | 16 |
| manifold | 2082 | 2 |
| manifold | 2083 | 2 |
| manifold | 2084 | 2 |
| manifold | 2085 | 6 |
| manifold | 2086 | 1 |
| manifold | 2087 | 4 |
| manifold | 2088 | 3 |
| manifold | 2089 | 5 |
| manifold | 2090 | 14 |
| manifold | 2091 | 9 |
| manifold | 2092 | 1 |
| manifold | 2094 | 2 |
| manifold | 2095 | 3 |
| manifold | 2096 | 3 |
| manifold | 2097 | 3 |
| manifold | 2098 | 3 |
| manifold | 2099 | 351 |
| manifold | 2100 | 553 |
| manifold | 2101 | 150 |
| manifold | 2102 | 4 |
| manifold | 2103 | 2 |
| manifold | 2104 | 2 |
| manifold | 2105 | 1 |
| manifold | 2106 | 1 |
| manifold | 2108 | 2 |
| manifold | 2109 | 1 |
| manifold | 2110 | 7 |
| manifold | 2111 | 3 |
| manifold | 2112 | 6 |
| manifold | 2113 | 1 |
| manifold | 2114 | 5 |
| manifold | 2115 | 1 |
| manifold | 2116 | 3 |

## Resolved YES/NO markets by venue and category (D-08)

| venue | category | markets |
|---|---|---|
| manifold | other | 92,600 |

Share of `other` among real-money resolved markets (DQ-15, must be below 15%): n/a.

## Excluded markets by reason

| venue | voided | scalar / 50-50 | disputed, unresolved | no final result | resolved after a dispute (flagged, kept) | < 10 trades | < USD 100 notional |
|---|---|---|---|---|---|---|---|
| manifold | 7,885 | 2,012 | 0 | 28,899 | 0 | 92,600 | 0 |

## Coverage gaps (resolved YES/NO markets)

| venue | no trades before close | no price history |
|---|---|---|
| manifold | 92,600 | 92,600 |

## Disk use (NFR-13: raw ≤ 20 GB, warehouse ≤ 30 GB)

| item | size |
|---|---|
| raw/fred | 0.00 GB |
| raw/kalshi | 10.23 GB |
| raw/manifold | 0.84 GB |
| raw/polymarket | 11.99 GB |
| raw total | 23.07 GB |
| interim | 15.33 GB |
| warehouse | 0.03 GB |
| free on the data filesystem | 666.42 GB |

## Warehouse rows

| table | rows |
|---|---|
| markets | 140,956 |
| trades | 0 |
| price_history | 0 |
| book_l2 | 0 |
| risk_free | 18,204 |
