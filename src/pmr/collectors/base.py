"""Shared collector machinery: raw writing, checkpoint commits and pagination (FR-08, NFR-02).

A :class:`Stream` pairs a :class:`RawWriter` with a :class:`Checkpoint`. Each page is written
together with the collector state *after* that page. When the writer flushes a part, the
latest state is committed to the checkpoint, so a checkpoint never claims a page that is not on
disk. After a kill, collection restarts from the last committed state; pages fetched since then
are fetched again, and the warehouse de-duplicates them (DQ-08).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from pmr.collectors.raw import Checkpoint, RawWriter, record_from_fetch
from pmr.http.client import FetchResult, HttpClient
from pmr.settings import PmrConfig, Settings

log = structlog.get_logger("pmr.collectors")


class CollectorError(RuntimeError):
    """Raised when a collector cannot run (missing token, storage not allowed, ...)."""


@dataclass
class IngestStats:
    """Counts for one ingest run."""

    pages: int = 0
    items: int = 0
    units_done: int = 0
    units_skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        """Plain dict for logs and the CLI summary."""
        return {
            "pages": self.pages,
            "items": self.items,
            "units_done": self.units_done,
            "units_skipped": self.units_skipped,
        }


@dataclass
class CollectContext:
    """Everything a collector needs."""

    http: HttpClient
    settings: Settings
    cfg: PmrConfig
    data_dir: Path
    since: date | None = None
    until: date | None = None
    flush_every: int = 200


@dataclass
class Stream:
    """Raw writer plus checkpoint for one venue endpoint."""

    ctx: CollectContext
    venue: str
    endpoint: str
    checkpoint_name: str = "default"
    writer: RawWriter = field(init=False)
    checkpoint: Checkpoint = field(init=False)
    _pending: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.writer = RawWriter(self.ctx.data_dir, self.venue, self.endpoint, max_records=10**9)
        self.checkpoint = Checkpoint.load(
            self.ctx.data_dir, self.venue, self.endpoint, self.checkpoint_name
        )
        self._pending = copy.deepcopy(self.checkpoint.state)

    @property
    def state(self) -> dict[str, Any]:
        """Working state (committed on the next flush)."""
        return self._pending

    def put(self, result: FetchResult, key: Mapping[str, Any] | None = None) -> None:
        """Write one page; flush and commit the state every ``flush_every`` pages."""
        self.writer.write(
            record_from_fetch(result, venue=self.venue, endpoint=self.endpoint, key=key)
        )
        if self.writer.buffered >= self.ctx.flush_every:
            self.commit()

    def commit(self) -> None:
        """Flush buffered pages, then persist the working state."""
        self.writer.flush()
        self.checkpoint.state = copy.deepcopy(self._pending)
        self.checkpoint.save()


def to_unix(d: date, *, end: bool = False) -> int:
    """UTC midnight of ``d`` in Unix seconds (``end`` gives the next midnight)."""
    dt = datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
    return int(dt) + (86400 if end else 0)


async def cursor_pages(
    ctx: CollectContext,
    stream: Stream,
    url: str,
    params: Mapping[str, str | int],
    *,
    next_cursor: Callable[[Any], str | None],
    items: Callable[[Any], int],
    cursor_param: str = "cursor",
    state_key: str = "cursor",
    key: Mapping[str, Any] | None = None,
    stats: IngestStats,
    max_pages: int | None = None,
) -> None:
    """Follow a cursor-paginated endpoint, resuming from ``stream.state[state_key]``.

    The state holds ``{"cursor": <next cursor>}`` or ``{"done": true}``.
    """
    st: dict[str, Any] = stream.state.setdefault(state_key, {})
    if st.get("done"):
        return
    cursor = st.get("cursor")
    pages = 0
    while True:
        p = dict(params)
        if cursor:
            p[cursor_param] = cursor
        res = await ctx.http.get(url, p)
        n = items(res.data)
        nxt = next_cursor(res.data)
        stats.pages += 1
        stats.items += n
        pages += 1
        if nxt and n > 0:
            stream.state[state_key] = {"cursor": nxt}
        else:
            stream.state[state_key] = {"done": True}
        stream.put(res, key)
        if not nxt or n == 0 or (max_pages is not None and pages >= max_pages):
            break
        cursor = nxt


def done_set(stream: Stream, name: str = "done") -> set[str]:
    """Ids of fully collected units (markets, tickers) in the working state."""
    return set(stream.state.get(name, []))


def mark_done(stream: Stream, unit: str, name: str = "done") -> None:
    """Add ``unit`` to the done list (sorted for stable checkpoints)."""
    s = done_set(stream, name)
    s.add(unit)
    stream.state[name] = sorted(s)


def chunks(seq: Iterable[str], n: int) -> list[list[str]]:
    """Split ``seq`` into lists of at most ``n``."""
    out: list[list[str]] = []
    cur: list[str] = []
    for x in seq:
        cur.append(x)
        if len(cur) == n:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out
