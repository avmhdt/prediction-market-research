"""Immutable raw archive and checkpoints (FR-08, DQ-12, NFR-02).

Layout: ``<data>/raw/<venue>/<endpoint>/date=YYYY-MM-DD/part-<ts_ns>-<n>.jsonl.zst``. Each
line is one HTTP response page::

    {"v": 1, "venue": ..., "endpoint": ..., "url": ..., "params": {...}, "status": 200,
     "ts_recv_ns": ..., "date_header": ..., "attempts": 1, "key": {...}, "body": <json>}

``params`` are already redacted by the HTTP client. A part is written to ``*.tmp`` and renamed
when closed, so a killed run never leaves a half-written part that a reader would pick up.
Parts are never modified after the rename.

Checkpoints live next to the parts in ``_checkpoint.json`` and are replaced atomically.
A collector closes its open part *before* writing a checkpoint, so every page that a
checkpoint says is done is already on disk.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

from pmr.http.client import FetchResult

RAW_VERSION = 1
PART_SUFFIX = ".jsonl.zst"


def raw_root(data_dir: Path) -> Path:
    """``<data>/raw``."""
    return Path(data_dir) / "raw"


def endpoint_dir(data_dir: Path, venue: str, endpoint: str) -> Path:
    """Directory holding one endpoint's parts and checkpoint."""
    return raw_root(data_dir) / venue / endpoint


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def record_from_fetch(
    result: FetchResult, *, venue: str, endpoint: str, key: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build one raw-archive record from an HTTP response."""
    return {
        "v": RAW_VERSION,
        "venue": venue,
        "endpoint": endpoint,
        "url": result.url,
        "params": dict(result.params),
        "status": result.status,
        "ts_recv_ns": result.ts_recv_ns,
        "date_header": result.date_header,
        "attempts": result.attempts,
        "key": dict(key or {}),
        "body": result.data,
    }


@dataclass
class RawWriter:
    """Appends records to compressed parts for one venue endpoint."""

    data_dir: Path
    venue: str
    endpoint: str
    max_records: int = 500
    _buf: list[bytes] = field(default_factory=list)
    _first_ts: int | None = None
    _seq: int = 0
    parts_written: list[Path] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        """Endpoint directory."""
        return endpoint_dir(self.data_dir, self.venue, self.endpoint)

    @property
    def buffered(self) -> int:
        """Records waiting to be flushed."""
        return len(self._buf)

    def write(self, record: Mapping[str, Any]) -> None:
        """Buffer one record; a full buffer is flushed to a new part."""
        if self._first_ts is None:
            self._first_ts = int(record["ts_recv_ns"])
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        self._buf.append(line.encode("utf-8") + b"\n")
        if len(self._buf) >= self.max_records:
            self.flush()

    def flush(self) -> Path | None:
        """Write the buffered records to a part (``.tmp`` then rename); return its path."""
        if not self._buf:
            return None
        ts = self._first_ts or 0
        day = datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d")
        d = self.directory / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        while True:
            path = d / f"part-{ts:019d}-{self._seq:04d}{PART_SUFFIX}"
            self._seq += 1
            if not path.exists():
                break
        tmp = path.with_name(path.name + ".tmp")
        cctx = zstandard.ZstdCompressor(level=10)
        tmp.write_bytes(cctx.compress(b"".join(self._buf)))
        os.replace(tmp, path)
        self._buf.clear()
        self._first_ts = None
        self.parts_written.append(path)
        return path


def iter_parts(data_dir: Path, venue: str | None = None, endpoint: str | None = None) -> list[Path]:
    """All closed raw parts, sorted by path (deterministic)."""
    root = raw_root(data_dir)
    pattern = f"{venue or '*'}/{endpoint or '*'}/date=*/part-*{PART_SUFFIX}"
    return sorted(root.glob(pattern))


def read_part(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of one part."""
    dctx = zstandard.ZstdDecompressor()
    with path.open("rb") as fh, dctx.stream_reader(fh) as reader:
        text = io.TextIOWrapper(reader, encoding="utf-8")
        for line in text:
            if line.strip():
                rec: dict[str, Any] = json.loads(line)
                yield rec


def part_id(path: Path) -> str:
    """Stable id of a part: ``<venue>/<endpoint>/<date>/<file stem>``."""
    rel = path.parts[-4:]
    return "/".join([*rel[:-1], rel[-1].removesuffix(PART_SUFFIX)])


@dataclass
class Checkpoint:
    """JSON state for resumable collection, replaced atomically."""

    path: Path
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path, venue: str, endpoint: str, name: str = "default") -> Checkpoint:
        """Load ``_checkpoint[-name].json`` (empty state when absent)."""
        fname = "_checkpoint.json" if name == "default" else f"_checkpoint-{name}.json"
        path = endpoint_dir(data_dir, venue, endpoint) / fname
        state: dict[str, Any] = {}
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, state=state)

    def save(self) -> None:
        """Persist the state."""
        _atomic_write_text(self.path, json.dumps(self.state, sort_keys=True, indent=1))

    def reset(self) -> None:
        """Forget all progress."""
        self.state = {}
        if self.path.exists():
            self.path.unlink()
