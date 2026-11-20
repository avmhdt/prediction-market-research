"""Decode Polymarket's Polygon logs (raw ``onchain_*`` parts) into interim tables.

Each raw part is decoded on its own (the normalizer stays a pure function of the raw archive);
the warehouse build joins the tables into ``markets`` and ``trades``
(:mod:`pmr.warehouse.onchain`). Event layouts and topic hashes: config/onchain.yaml and
docs/venue_notes.md.

Fills (taker rows only, one per match): V1 ``OrderFilled`` data is (makerAssetId,
takerAssetId, makerAmountFilled, takerAmountFilled, fee); asset id 0 is collateral. V2/V3 data
is (side, tokenId, makerAmountFilled, takerAmountFilled, fee, builder, metadata) with side 0 =
BUY. All amounts have 6 decimals. ``side`` here is +1 when the taker bought the token.
"""

from __future__ import annotations

import json
import re
from typing import Any

import polars as pl

T = {
    "fill_v1": "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6",
    "fill_v2": "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
    "prep": "0xab3760c3bd2bb38b5bcf54dc79802ed67338b4cf29f3054ded67ed24661e4177",
    "resolution": "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894",
    "q_init": "0xeee0897acd6893adcaf2ba5158191b3601098ab6bece35c5d57874340b64c5b7",
    "q_resolved": "0x566c3fbdd12dd86bb341787f6d531f79fd7ad4ce7e3ae2d15ac0ca1b601af9df",
    "q_reset": "0x7981b5832932948db4e32a4a16a0f44b2ce7ff088574afb9364b313f70f82e8f",
    "q_manual": "0x5909815fe7fe0a550d5fcb95fbf33821b580521d3c97089c6ce12808d1cd0566",
    "q_flagged": "0x2435a0347185933b12027c6f394a5fd9c03646dba233e956f50658719dfc0b35",
    "nr_market": "0xf059ab16d1ca60e123eab60e3c02b68faf060347c701a5d14885a8e1def7b3a8",
    "nr_question": "0xaac410f87d423a922a7b226ac68f0c2eaf5bf6d15e644ac0758c7f96e2c253f7",
    "token_registered": "0xbc9a2432e8aeb48327246cddd6e872ef452812b4243c04e6bfb786a2cd8faf0d",
    "v2_request": "0x459464e3d93204e986f0b20e33596daba585b1f37d5afe1eb1821551f7cb6f49",
    "v2_resolved": "0xe560c1a2847d4cf2e66de11fe592605556989449f38cc3ab96e2e287daa68caa",
    "v2_migration": "0x0da49435dfd3b856758ee0b1a2500f1e8062fed24dd654594af9fa163cc5bcb4",
}
UNIT = 1_000_000

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "oc_conditions": {
        "condition_id": pl.String(),
        "oracle": pl.String(),
        "question_id": pl.String(),
        "slots": pl.Int64(),
        "ts": pl.Int64(),
    },
    "oc_resolutions": {
        "condition_id": pl.String(),
        "oracle": pl.String(),
        "payout0": pl.Float64(),
        "payout1": pl.Float64(),
        "ts": pl.Int64(),
    },
    "oc_questions": {
        "question_id": pl.String(),
        "adapter": pl.String(),
        "kind": pl.String(),
        "title": pl.String(),
        "rules": pl.String(),
        "request_ts": pl.Int64(),
        "ts": pl.Int64(),
    },
    "oc_tokens": {"condition_id": pl.String(), "token_a": pl.String(), "token_b": pl.String()},
    "oc_fills": {
        "trade_id": pl.String(),
        "ts": pl.Int64(),
        "exchange": pl.String(),
        "layout": pl.String(),
        "token_id": pl.String(),
        "price": pl.Float64(),
        "qty": pl.Float64(),
        "side": pl.Int8(),
        "fee": pl.Float64(),
        "ingested_at": pl.Int64(),
    },
}


def words(data: str) -> list[int]:
    """ABI data as 32-byte words."""
    h = data.removeprefix("0x")
    return [int(h[i : i + 64], 16) for i in range(0, len(h), 64)]


def dyn_bytes(data: str, offset: int) -> bytes:
    """A dynamic ``bytes`` value at ``offset`` (in bytes) of ABI data."""
    h = data.removeprefix("0x")
    n = int(h[offset * 2 : offset * 2 + 64], 16)
    start = offset * 2 + 64
    return bytes.fromhex(h[start : start + n * 2])


def dyn_uints(data: str, offset: int) -> list[int]:
    """A dynamic ``uint256[]`` at ``offset`` (in bytes) of ABI data."""
    w = words(data)
    i = offset // 32
    n = w[i]
    return w[i + 1 : i + 1 + n]


def topic_addr(topic: str) -> str:
    """Address from a 32-byte topic (lower case)."""
    return "0x" + topic[-40:].lower()


def _ts(log: dict[str, Any]) -> int | None:
    t = log.get("blockTimestamp")
    return int(t, 16) * 10**9 if t else None


TITLE = re.compile(r"title:\s*(.*?),\s*description:\s*", re.S)


def parse_ancillary(text: str) -> tuple[str | None, str]:
    """``(title, rules)`` from UMA ancillary data (``q: title: ..., description: ...``)."""
    body = text.split(",initializer:")[0]
    m = TITLE.search(body)
    if not m:
        return None, body.strip()
    rules = body[m.end() :]
    rules = re.split(r"\s*res_data:", rules)[0]
    return m.group(1).strip(), rules.strip()


def parse_question_data(text: str) -> tuple[str | None, str]:
    """``(title, rules)`` from NegRisk ``question: ..., description: ...`` data."""
    m = re.search(r"question:\s*(.*?),\s*description:\s*", text, re.S)
    if not m:
        return None, text.strip()
    return m.group(1).strip(), text[m.end() :].strip()


def _fill(log: dict[str, Any], layout: str, ts_recv: int) -> dict[str, Any] | None:
    w = words(log.get("data", "0x"))
    ts = _ts(log)
    if ts is None or len(w) < 5:
        return None
    if layout == "v1":
        maker_asset, taker_asset, maker_amt, taker_amt, fee = w[:5]
        if maker_asset == 0 and taker_amt:
            token, price, qty, side = taker_asset, maker_amt / taker_amt, taker_amt, 1
        elif taker_asset == 0 and maker_amt:
            token, price, qty, side = maker_asset, taker_amt / maker_amt, maker_amt, -1
        else:
            return None
    else:
        side_raw, token, maker_amt, taker_amt, fee = w[:5]
        if side_raw == 0 and taker_amt:
            price, qty, side = maker_amt / taker_amt, taker_amt, 1
        elif side_raw == 1 and maker_amt:
            price, qty, side = taker_amt / maker_amt, maker_amt, -1
        else:
            return None
    if not 0.0 <= price <= 1.0:
        return None
    return {
        "trade_id": f"{log.get('transactionHash')}:{int(log.get('logIndex', '0x0'), 16)}",
        "ts": ts,
        "exchange": str(log.get("address", "")).lower(),
        "layout": layout,
        "token_id": str(token),
        "price": price,
        "qty": qty / UNIT,
        "side": side,
        "fee": fee / UNIT,
        "ingested_at": ts_recv,
    }


def decode(rec: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """All interim rows from one raw ``onchain_*`` record (one JSON-RPC response)."""
    body = rec.get("body") or {}
    logs = body.get("result") or [] if isinstance(body, dict) else []
    ts_recv = int(rec["ts_recv_ns"])
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in SCHEMAS}
    for log in logs:
        if not isinstance(log, dict) or log.get("removed"):
            continue
        topics = [str(t).lower() for t in log.get("topics") or []]
        if not topics:
            continue
        t0, data, ts = topics[0], str(log.get("data", "0x")), _ts(log)
        addr = str(log.get("address", "")).lower()
        if t0 in (T["fill_v1"], T["fill_v2"]):
            layout = "v1" if t0 == T["fill_v1"] else ("v3" if addr.startswith("0xe33337") else "v2")
            row = _fill(log, layout, ts_recv)
            if row:
                out["oc_fills"].append(row)
        elif t0 == T["prep"] and len(topics) >= 4:
            out["oc_conditions"].append(
                {
                    "condition_id": topics[1],
                    "oracle": topic_addr(topics[2]),
                    "question_id": topics[3],
                    "slots": words(data)[0],
                    "ts": ts,
                }
            )
        elif t0 == T["resolution"] and len(topics) >= 4:
            payouts = dyn_uints(data, words(data)[1])
            total = sum(payouts) or 1
            if len(payouts) == 2:
                out["oc_resolutions"].append(
                    {
                        "condition_id": topics[1],
                        "oracle": topic_addr(topics[2]),
                        "payout0": payouts[0] / total,
                        "payout1": payouts[1] / total,
                        "ts": ts,
                    }
                )
        elif t0 == T["q_init"] and len(topics) >= 3:
            text = dyn_bytes(data, words(data)[0]).decode("utf-8", errors="replace")
            title, rules = parse_ancillary(text)
            out["oc_questions"].append(
                {
                    "question_id": topics[1],
                    "adapter": addr,
                    "kind": "uma",
                    "title": title,
                    "rules": rules,
                    "request_ts": int(topics[2], 16) * 10**9,
                    "ts": ts,
                }
            )
        elif t0 in (T["q_reset"], T["q_flagged"]) and len(topics) >= 2:
            kind = "reset" if t0 == T["q_reset"] else "flagged"
            out["oc_questions"].append(
                {"question_id": topics[1], "adapter": addr, "kind": kind, "ts": ts}
            )
        elif t0 == T["nr_question"] and len(topics) >= 3:
            w = words(data)
            text = dyn_bytes(data, w[1]).decode("utf-8", errors="replace")
            title, rules = parse_question_data(text)
            out["oc_questions"].append(
                {
                    "question_id": topics[2],
                    "adapter": addr,
                    "kind": "negrisk",
                    "title": title,
                    "rules": rules,
                    "ts": ts,
                }
            )
        elif t0 == T["token_registered"] and len(topics) >= 4:
            out["oc_tokens"].append(
                {
                    "condition_id": topics[3],
                    "token_a": str(int(topics[1], 16)),
                    "token_b": str(int(topics[2], 16)),
                }
            )
        elif t0 == T["v2_request"] and len(topics) >= 2:
            raw = dyn_bytes(data, words(data)[0]).decode("utf-8", errors="replace")
            try:
                rules_json = json.loads(raw)
            except ValueError:
                rules_json = {}
            out["oc_questions"].append(
                {
                    "question_id": topics[1],
                    "adapter": addr,
                    "kind": "v2",
                    "title": rules_json.get("title") if isinstance(rules_json, dict) else None,
                    "rules": rules_json.get("description") if isinstance(rules_json, dict) else raw,
                    "ts": ts,
                }
            )
        elif t0 == T["v2_resolved"] and len(topics) >= 2:
            res = dyn_uints(data, words(data)[0])
            if len(res) == 2:
                total = sum(res) or 1
                out["oc_resolutions"].append(
                    {
                        "condition_id": topics[1],
                        "oracle": addr,
                        "payout0": res[0] / total,
                        "payout1": res[1] / total,
                        "ts": ts,
                    }
                )
    return out


def frames(rows: dict[str, list[dict[str, Any]]]) -> dict[str, pl.DataFrame]:
    """Typed frames for the non-empty interim tables."""
    out: dict[str, pl.DataFrame] = {}
    for name, rs in rows.items():
        if rs:
            schema = SCHEMAS[name]
            out[name] = pl.DataFrame({c: [r.get(c) for r in rs] for c in schema}, schema=schema)
    return out
