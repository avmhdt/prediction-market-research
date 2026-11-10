"""Paper ledger (FR-24): JSONL event log, positions, daily marks and settlement.

Events go to ``<data>/paper/paper-YYYY-MM-DD.jsonl``, one JSON object per line, with types
``order_intent``, ``fill``, ``opportunity``, ``opportunity_seen``, ``mark``, ``settlement``,
``gap`` and ``set_change``. Every event carries the paper config hash. No field ever holds a
venue order id: nothing is sent to a venue.

Positions are YES units per (strategy, venue, symbol): ``q`` contracts, ``cash`` (USD) and
``fees``. Marks value open positions at the mid once per UTC day; a resolved market settles at
its settlement value.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Position:
    """One strategy's position in one market."""

    q: float = 0.0
    cash: float = 0.0
    fees: float = 0.0
    unknown_fee_fills: int = 0


@dataclass
class Ledger:
    """Writes events and keeps positions."""

    directory: Path
    config_hash: str
    positions: dict[tuple[str, str, str], Position] = field(default_factory=dict)
    last_mark_day: str | None = None

    def _path(self, ts: int) -> Path:
        day = datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d")
        return self.directory / f"paper-{day}.jsonl"

    def write(self, events: Iterable[dict[str, Any]]) -> int:
        """Append events (and apply fills to positions); returns the count."""
        n = 0
        self.directory.mkdir(parents=True, exist_ok=True)
        by_file: dict[Path, list[str]] = {}
        for e in events:
            e = {**e, "config_hash": self.config_hash}
            if e.get("type") == "fill":
                self._apply(e)
            by_file.setdefault(self._path(int(e["ts"])), []).append(
                json.dumps(e, sort_keys=True, separators=(",", ":"))
            )
            n += 1
        for path, lines in by_file.items():
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
        return n

    def _apply(self, e: dict[str, Any]) -> None:
        key = (str(e["strategy"]), str(e["venue"]), str(e["symbol"]))
        pos = self.positions.setdefault(key, Position())
        side, price, qty = int(e["side"]), float(e["price"]), float(e["qty"])
        pos.q += side * qty
        pos.cash -= side * price * qty
        if e.get("fee") is None:
            pos.unknown_fee_fills += 1
        else:
            pos.fees += float(e["fee"])

    def marks(self, ts: int, mids: dict[str, float]) -> list[dict[str, Any]]:
        """Daily marks to mid (once per UTC day, on the first cycle of the day)."""
        day = datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d")
        if day == self.last_mark_day:
            return []
        self.last_mark_day = day
        out = []
        for (strategy, venue, symbol), pos in sorted(self.positions.items()):
            mid = mids.get(f"{venue}:{symbol}")
            if mid is None or abs(pos.q) < 1e-12 and abs(pos.cash) < 1e-12:
                continue
            out.append(
                {
                    "type": "mark",
                    "ts": ts,
                    "strategy": strategy,
                    "venue": venue,
                    "symbol": symbol,
                    "q": pos.q,
                    "mid": mid,
                    "cash": pos.cash,
                    "fees": pos.fees,
                    "value": pos.cash + pos.q * mid - pos.fees,
                }
            )
        return out

    def settle(self, ts: int, key: str, value: float) -> list[dict[str, Any]]:
        """Settle every strategy's position in ``venue:symbol`` at ``value`` per YES contract."""
        venue, symbol = key.split(":", 1)
        out = []
        for (strategy, v, s), pos in sorted(self.positions.items()):
            if (v, s) != (venue, symbol) or (abs(pos.q) < 1e-12 and abs(pos.cash) < 1e-12):
                continue
            pnl = pos.cash + pos.q * value - pos.fees
            out.append(
                {
                    "type": "settlement",
                    "ts": ts,
                    "strategy": strategy,
                    "venue": v,
                    "symbol": s,
                    "q": pos.q,
                    "value_per_contract": value,
                    "pnl": pnl,
                    "fees": pos.fees,
                    "unknown_fee_fills": pos.unknown_fee_fills,
                }
            )
            pos.cash += pos.q * value
            pos.q = 0.0
        return out


def restore(ledger: Ledger) -> set[str]:
    """Rebuild positions from the JSONL log after a restart.

    Returns the ``venue:symbol`` keys the FLB tilt already traded (it trades once per market).
    """
    flb_done: set[str] = set()
    for path in sorted(ledger.directory.glob("paper-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                e = json.loads(line)
                t = e.get("type")
                if t == "fill":
                    ledger._apply(e)
                    if e.get("strategy") == "flb_tilt":
                        flb_done.add(f"{e['venue']}:{e['symbol']}")
                elif t == "settlement":
                    pos = ledger.positions.get((e["strategy"], e["venue"], e["symbol"]))
                    if pos is not None:
                        pos.cash += pos.q * float(e["value_per_contract"])
                        pos.q = 0.0
                elif t == "mark":
                    ledger.last_mark_day = datetime.fromtimestamp(
                        int(e["ts"]) / 1e9, tz=UTC
                    ).strftime("%Y-%m-%d")
    return flb_done
