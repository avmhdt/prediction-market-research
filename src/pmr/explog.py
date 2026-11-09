"""Experiment log ``results/experiment_log.csv`` (FR-28): the honest trial count.

Every analysis, simulation and scan appends one row: UTC timestamp, git hash, dirty flag,
command, config hash, params JSON, sample size and headline metrics JSON.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH = Path("results/experiment_log.csv")
COLUMNS = (
    "ts_utc",
    "git_hash",
    "git_dirty",
    "command",
    "config_hash",
    "params_json",
    "n",
    "metrics_json",
)


def git_state(cwd: Path | None = None) -> tuple[str, bool]:
    """Return ``(commit hash, dirty flag)``; ``("unknown", True)`` outside a git checkout."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    return head, bool(status)


def config_hash(*paths: Path) -> str:
    """SHA-256 (first 16 hex chars) over the bytes of the given config files, in order."""
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).name.encode())
        h.update(b"\0")
        h.update(Path(p).read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _dumps(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def append(
    command: str,
    *,
    params: Mapping[str, Any],
    n: int,
    metrics: Mapping[str, Any],
    cfg_hash: str = "",
    path: Path = DEFAULT_LOG_PATH,
    now: datetime | None = None,
    repo: Path | None = None,
) -> dict[str, str]:
    """Append one row to the experiment log and return it."""
    git_hash, dirty = git_state(repo)
    row = {
        "ts_utc": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_hash": git_hash,
        "git_dirty": str(dirty).lower(),
        "command": command,
        "config_hash": cfg_hash,
        "params_json": _dumps(params),
        "n": str(n),
        "metrics_json": _dumps(metrics),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerow(row)
    return row
