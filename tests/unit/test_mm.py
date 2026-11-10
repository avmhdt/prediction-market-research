"""AC-W-08: inventory limits, the P&L identity, fills and a golden replay."""

from __future__ import annotations

import csv
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmr.sim.fills import Quote, passive_fill, taker_fill
from pmr.sim.mm import MMParams, MMStrategy, simulate_mm

S = 10**9
PARAMS = MMParams(
    half_spread=0.02,
    skew_gamma=0.0005,
    q_max=100,
    capital_max=1000,
    ema_halflife_s=300,
    quote_size=10,
)
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "mm_golden.csv"


def snap(bid: float, ask: float, qb: float = 50, qa: float = 50) -> pl.DataFrame:
    return pl.DataFrame(
        [(1, bid, qb), (-1, ask, qa)], schema=["side", "price", "qty"], orient="row"
    )


def test_passive_fill_rules() -> None:
    q = Quote(side=1, price=0.40, size=10, queue_ahead=5, posted_ts=0)
    assert passive_fill(q, 0.41, 100, -1) == 0  # above our bid: no fill
    assert passive_fill(q, 0.40, 3, 1) == 0  # at our bid but buyer-initiated
    assert passive_fill(q, 0.40, 3, 0) == 0  # unknown side never fills at the quote
    assert passive_fill(q, 0.40, 4, -1) == 0 and q.queue_ahead == 1  # queue first
    assert passive_fill(q, 0.40, 4, -1) == 3 and q.size == 7
    assert passive_fill(q, 0.39, 100, 0) == 7 and q.size == 0  # trade-through
    assert passive_fill(q, 0.30, 5, -1) == 0  # nothing left
    a = Quote(side=-1, price=0.60, size=10, queue_ahead=0, posted_ts=0)
    assert passive_fill(a, 0.60, 4, 1) == 4 and passive_fill(a, 0.61, 100, -1) == 6
    assert taker_fill([(0.5, 3), (0.6, 10)], 5) == [(0.5, 3), (0.6, 2)]
    assert taker_fill([(0.5, 3)], 0) == []


def test_fair_value_ema_and_quotes() -> None:
    m = MMStrategy(PARAMS)
    m.on_book(0, snap(0.48, 0.52))
    assert m.fair == pytest.approx(0.50)
    assert m.bid is not None and m.ask is not None
    assert (m.bid.price, m.ask.price) == (pytest.approx(0.48), pytest.approx(0.52))
    assert m.bid.queue_ahead == 50  # joined the best bid level
    m.on_book(300 * S, snap(0.58, 0.62))  # one half-life later: halfway to the new mid
    assert m.fair == pytest.approx(0.55)
    m.on_book(301 * S, snap(0.5, 0.5))  # locked book: fair unchanged
    assert m.fair == pytest.approx(0.55)


def test_one_sided_book_and_post_only() -> None:
    m = MMStrategy(PARAMS)
    m.on_book(0, pl.DataFrame({"side": [1], "price": [0.5], "qty": [1.0]}))
    assert m.fair is None and m.bid is None
    m.on_book(1, snap(0.40, 0.41))
    assert m.bid is not None and m.ask is not None
    assert m.bid.price < 0.41 and m.ask.price > 0.40


def test_identity_and_settlement_on_a_small_path() -> None:
    m = MMStrategy(PARAMS, fee_fn=lambda ts, p, q: 0.001 * q)
    m.on_book(0, snap(0.48, 0.52))
    m.on_trade(1 * S, 0.47, 30, -1)  # through our 0.48 bid: we buy 10
    m.on_book(400 * S, snap(0.40, 0.44))
    m.on_trade(401 * S, 0.47, 5, 1)  # through our ask
    res = m.result(settlement=1.0)
    comp = res.spread_capture + res.inventory_mtm + res.settlement - res.fees
    assert abs(res.total - comp) < 1e-9
    assert res.q_end == pytest.approx(10 - 5)
    assert res.fees == pytest.approx(0.015)
    assert res.markout_n[300] == 1 and res.markout_n[3600] == 0


trade = st.tuples(st.floats(0.01, 0.99), st.floats(0.1, 200.0), st.sampled_from([-1, 0, 1]))
step = st.tuples(st.floats(0.02, 0.97), st.floats(0.005, 0.05), st.lists(trade, max_size=4))


@settings(max_examples=80, deadline=None)
@given(st.lists(step, min_size=1, max_size=25), st.integers(1, 40), st.floats(5.0, 300.0))
def test_inventory_never_exceeds_limits_and_pnl_adds_up(
    steps: list[tuple[float, float, list[tuple[float, float, int]]]], q_max: int, cap: float
) -> None:
    p = MMParams(
        half_spread=0.01,
        skew_gamma=0.001,
        q_max=q_max,
        capital_max=cap,
        ema_halflife_s=60,
        quote_size=7,
    )
    m = MMStrategy(p, fee_fn=lambda ts, px, q: 0.0001 * q)
    ts = 0
    for mid, half, trades in steps:
        ts += 30 * S
        m.on_book(ts, snap(max(0.01, mid - half), min(0.99, mid + half)))
        for price, qty, side in trades:
            ts += S
            m.on_trade(ts, price, qty, side)
            assert abs(m.q) <= q_max + 1e-9
    res = m.result()
    assert (
        abs(res.total - (res.spread_capture + res.inventory_mtm + res.settlement - res.fees)) < 1e-9
    )


def _golden_inputs() -> tuple[pl.DataFrame, pl.DataFrame]:
    rows, trades = [], []
    mids = [0.50, 0.51, 0.53, 0.52, 0.49, 0.47, 0.50, 0.55, 0.56, 0.54]
    for i, mid in enumerate(mids):
        ts = i * 120 * S
        for side, px in ((1, round(mid - 0.02, 2)), (-1, round(mid + 0.02, 2))):
            rows.append(
                {
                    "venue": "kalshi",
                    "symbol": "G",
                    "seq": i + 1,
                    "ts_recv": ts,
                    "side": side,
                    "price": px,
                    "qty": 20.0,
                }
            )
        trades.append(
            {
                "venue": "kalshi",
                "symbol": "G",
                "trade_id": f"t{i}",
                "ts_exchange": ts + 60 * S,
                "price": round(mid - 0.03, 2),
                "qty": 15.0,
                "side": -1,
            }
        )
        trades.append(
            {
                "venue": "kalshi",
                "symbol": "G",
                "trade_id": f"u{i}",
                "ts_exchange": ts + 90 * S,
                "price": round(mid + 0.03, 2),
                "qty": 8.0,
                "side": 1,
            }
        )
    return pl.DataFrame(rows), pl.DataFrame(trades)


def test_golden_replay_reproduced() -> None:
    books, trades = _golden_inputs()
    res = simulate_mm(
        books,
        trades,
        PARAMS,
        venue="kalshi",
        symbol="G",
        fee_fn=lambda ts, p, q: 0.0,
        settlement=1.0,
    )
    got = {
        "n_fills": len(res.fills),
        "q_end": round(res.q_end, 9),
        "spread_capture": round(res.spread_capture, 9),
        "inventory_mtm": round(res.inventory_mtm, 9),
        "settlement": round(res.settlement, 9),
        "fees": round(res.fees, 9),
        "total": round(res.total, 9),
        "markout_300": round(res.markouts[300], 9),
    }
    with GOLDEN.open(encoding="utf-8") as fh:
        want = {r["metric"]: float(r["value"]) for r in csv.DictReader(fh)}
    assert {k: float(v) for k, v in got.items()} == pytest.approx(want, abs=1e-9)
