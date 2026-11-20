"""Decoding on-chain logs into markets and YES-oriented trades (hand-encoded ABI data)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from pmr.collectors.raw import RawWriter
from pmr.normalize import categories
from pmr.normalize.ctf import NEG_RISK_WCOL, USDC_E, binary_tokens, condition_id
from pmr.normalize.polymarket_onchain import T, decode, parse_ancillary, parse_question_data
from pmr.normalize.run import run as normalize
from pmr.warehouse.build import build
from pmr.warehouse.onchain import NEG_RISK_ADAPTER, check_tokens, token_map
from tests.conftest import REPO

COND = "0x849753c23a361b9c8696072789fb39bb8fa526ffcbab52f7c439f43dee54b1bb"
YES_TOKEN = 33835467767079395729341572182569300859577488907331778086137490953361052119998
QID = "0x" + "ab" * 31 + "07"
EX1 = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
EX2 = "0xe111180000d2663c0091e4f400237545b87b996b"
TS = 1_718_000_000


def w(*vals: int) -> str:
    return "".join(f"{v:064x}" for v in vals)


def bytes_word(b: bytes) -> str:
    padded = b + b"\0" * ((32 - len(b) % 32) % 32)
    return f"{len(b):064x}" + padded.hex()


def t32(x: int | str) -> str:
    if isinstance(x, int):
        return "0x" + f"{x:064x}"
    return "0x" + x.removeprefix("0x").rjust(64, "0")


def log(address: str, topics: list[str], data: str, n: int) -> dict[str, Any]:
    return {
        "address": address,
        "topics": topics,
        "data": "0x" + data,
        "blockTimestamp": hex(TS + n),
        "transactionHash": f"0xtx{n}",
        "logIndex": hex(n),
    }


def logs() -> list[dict[str, Any]]:
    yes, no = binary_tokens(COND, NEG_RISK_WCOL)
    q_text = b"question: Will Boston win the 2024 title?, description: Resolves Yes if Boston wins."
    return [
        log(
            "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
            [T["prep"], COND, t32(NEG_RISK_ADAPTER), QID],
            w(2),
            1,
        ),
        log(
            NEG_RISK_ADAPTER,
            [T["nr_question"], t32("0x" + "ab" * 31 + "00"), QID],
            w(7, 64) + bytes_word(q_text),
            2,
        ),
        log(EX1, [T["token_registered"], t32(no), t32(yes), COND], "", 3),
        # V1 taker buys the YES token: maker asset 0 (cash) 60 USDC for 100 tokens.
        log(
            EX1, [T["fill_v1"], t32(1), t32(5), t32(EX1)], w(0, yes, 60_000_000, 100_000_000, 0), 4
        ),
        # V2 taker sells the NO token: 50 tokens for 15 USDC -> NO at 0.30 = YES 0.70, side +1.
        log(
            EX2,
            [T["fill_v2"], t32(2), t32(6), t32(EX2)],
            w(1, no, 50_000_000, 15_000_000, 0, 0, 0),
            5,
        ),
        log(
            "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
            [T["resolution"], COND, t32(NEG_RISK_ADAPTER), QID],
            w(2, 64, 2, 1, 0),
            6,
        ),
    ]


def test_ctf_derivation_matches_a_real_polymarket_token() -> None:
    assert binary_tokens(COND, NEG_RISK_WCOL)[0] == YES_TOKEN
    assert binary_tokens(COND, USDC_E)[0] != YES_TOKEN
    assert (
        condition_id("0x" + "11" * 20, QID).startswith("0x")
        and len(condition_id("0x" + "11" * 20, QID)) == 66
    )


def test_text_parsers() -> None:
    title, rules = parse_ancillary(
        "q: title: Will X happen?, description: This resolves Yes if X. res_data: p1: 0, "
        "p2: 1, p3: 0.5,initializer:abc"
    )
    assert title == "Will X happen?" and rules == "This resolves Yes if X."
    assert parse_ancillary("no structure") == (None, "no structure")
    assert parse_question_data("question: Q?, description: D.") == ("Q?", "D.")
    assert parse_question_data("plain") == (None, "plain")


def test_decode_and_build(tmp_path: Path) -> None:
    rec = {"ts_recv_ns": 1, "body": {"result": logs()}}
    rows = decode(rec)
    assert len(rows["oc_fills"]) == 2 and len(rows["oc_conditions"]) == 1
    buy = rows["oc_fills"][0]
    assert buy["price"] == pytest.approx(0.6) and buy["qty"] == 100 and buy["side"] == 1
    w_ = RawWriter(tmp_path, "polymarket", "onchain_test", 10**9)
    w_.write(
        {
            "v": 1,
            "venue": "polymarket",
            "endpoint": "onchain_test",
            "url": "x",
            "params": {},
            "status": 200,
            "ts_recv_ns": TS * 10**9,
            "date_header": None,
            "attempts": 1,
            "key": {},
            "body": {"result": logs()},
        }
    )
    w_.flush()
    cats = categories.load(str(REPO / "config" / "categories.yaml"))
    normalize(tmp_path, cats)
    build(tmp_path, tmp_path / "w.duckdb", cats, threads=1)
    con = duckdb.connect(str(tmp_path / "w.duckdb"), read_only=True)
    m = con.execute(
        "SELECT symbol, result, title, event_id, close_ts, category FROM markets_current"
    ).fetchall()
    assert m == [
        (
            str(YES_TOKEN),
            "YES",
            "Will Boston win the 2024 title?",
            "0x" + "ab" * 31 + "00",
            (TS + 6) * 10**9,
            "other",
        )
    ]
    t = con.execute(
        "SELECT price, side, qty, symbol, source FROM trades ORDER BY ts_exchange"
    ).fetchall()
    assert t[0] == (pytest.approx(0.6), 1, 100.0, str(YES_TOKEN), "onchain")
    assert t[1][:3] == (pytest.approx(0.7), 1, 50.0) and t[1][3] == str(YES_TOKEN)
    con.close()


def test_token_check_flags_mismatches() -> None:
    import polars as pl

    cond = pl.DataFrame(
        {
            "condition_id": [COND],
            "oracle": [NEG_RISK_ADAPTER],
            "question_id": [QID],
            "slots": [2],
            "ts": [1],
        }
    )
    tmap = token_map(cond)
    yes, no = binary_tokens(COND, NEG_RISK_WCOL)
    good = pl.DataFrame({"condition_id": [COND], "token_a": [str(no)], "token_b": [str(yes)]})
    assert check_tokens(tmap, good) == (1, 0)
    bad = pl.DataFrame({"condition_id": [COND], "token_a": ["1"], "token_b": [str(yes)]})
    assert check_tokens(tmap, bad) == (1, 1)
    assert check_tokens(tmap, bad.clear()) == (0, 0)
