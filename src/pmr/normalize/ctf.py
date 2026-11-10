"""Gnosis Conditional Tokens id derivation (port of CTHelpers.sol), for Polymarket on-chain data.

``getCollectionId`` maps (condition, index set) to a point on alt_bn128 (y^2 = x^3 + 3 mod P):
x starts at keccak256(conditionId, indexSet), is incremented until x^3 + 3 is a square, y's
parity follows the hash's top bit, and the result is x with bit 254 set when y is odd. A
position (ERC-1155 token) id is keccak256(collateral, collectionId). Outcome index i of a
binary condition has index set ``1 << i``, so outcome 0 (the question's first label, "Yes")
is index set 1. Validated against the token ids of real Polymarket markets in the tests.
"""

from __future__ import annotations

from Crypto.Hash import keccak

P = 21888242871839275222246405745257275088696311157297823662689037894645226208583
B = 3
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_WCOL = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"


def keccak256(data: bytes) -> bytes:
    """Keccak-256 digest."""
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _b32(hex_or_int: str | int) -> bytes:
    if isinstance(hex_or_int, int):
        return hex_or_int.to_bytes(32, "big")
    return bytes.fromhex(hex_or_int.removeprefix("0x").rjust(64, "0"))


def collection_id(condition_id: str, index_set: int) -> int:
    """``CTHelpers.getCollectionId(0x0, conditionId, indexSet)``."""
    x1 = int.from_bytes(keccak256(_b32(condition_id) + _b32(index_set)), "big")
    odd = (x1 >> 255) != 0
    while True:
        x1 = (x1 + 1) % P
        yy = (x1 * x1 % P * x1 + B) % P
        y1 = pow(yy, (P + 1) // 4, P)
        if y1 * y1 % P == yy:
            break
    if (odd and y1 % 2 == 0) or (not odd and y1 % 2 == 1):
        y1 = P - y1
    if y1 % 2 == 1:
        x1 ^= 1 << 254
    return x1


def position_id(collateral: str, collection: int) -> int:
    """``CTHelpers.getPositionId(collateral, collectionId)`` (the ERC-1155 token id)."""
    addr = bytes.fromhex(collateral.removeprefix("0x"))
    return int.from_bytes(keccak256(addr + _b32(collection)), "big")


def binary_tokens(condition_id: str, collateral: str = USDC_E) -> tuple[int, int]:
    """(outcome-0 token, outcome-1 token) of a binary condition."""
    return (
        position_id(collateral, collection_id(condition_id, 1)),
        position_id(collateral, collection_id(condition_id, 2)),
    )


def condition_id(oracle: str, question_id: str, outcome_slots: int = 2) -> str:
    """``keccak256(abi.encodePacked(oracle, questionId, outcomeSlotCount))``."""
    data = bytes.fromhex(oracle.removeprefix("0x")) + _b32(question_id) + _b32(outcome_slots)
    return "0x" + keccak256(data).hex()
