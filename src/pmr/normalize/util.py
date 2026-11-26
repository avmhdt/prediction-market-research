"""Small conversions shared by the venue normalizers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

NS = 1_000_000_000
MAX_NS = 2**63 - 1


def _clamp(ns: int) -> int | None:
    """None for timestamps outside int64 nanoseconds (for example a close time in 2400)."""
    return ns if -MAX_NS <= ns <= MAX_NS else None


def iso_ns(value: Any) -> int | None:
    """ISO-8601 string (``Z``, offset, or ``+00`` suffix, space or ``T``) to ns UTC."""
    if value is None or value == "":
        return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 3 and s[-3] in "+-" and s[-3:].lstrip("+-").isdigit() and ":" not in s[-3:]:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return _clamp(int(dt.timestamp() * 1_000_000) * 1000)


def unix_s_ns(value: Any) -> int | None:
    """Unix seconds (int, float or numeric string) to ns."""
    if value is None or value == "":
        return None
    return _clamp(int(round(float(value) * 1_000_000)) * 1000)


def unix_ms_ns(value: Any) -> int | None:
    """Unix milliseconds to ns."""
    if value is None or value == "":
        return None
    return _clamp(int(value) * 1_000_000)


def fnum(value: Any) -> float | None:
    """Float from a number or numeric string; None for blanks and junk."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def json_list(value: Any) -> list[Any]:
    """A list from a list or a JSON-encoded list string (Gamma encodes several fields)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            out = json.loads(value)
        except ValueError:
            return []
        return out if isinstance(out, list) else []
    return []


def rules_hash(*parts: str | None) -> str:
    """SHA-256 (first 16 hex chars) of the rules texts, joined with a separator."""
    h = hashlib.sha256("\x1f".join(p or "" for p in parts).encode("utf-8"))
    return h.hexdigest()[:16]


def stable_id(*parts: Any) -> str:
    """Deterministic id from parts (SHA-1, first 24 hex chars)."""
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[
        :24
    ]
