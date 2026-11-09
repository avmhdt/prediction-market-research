"""NFR-08 repository hygiene: no raw or licensed data in git.

Fails if any tracked file is under data/, ends in .duckdb/.parquet/.jsonl/.zst/.pem,
or is larger than 500 KB outside reports/figures/. Usage: ``python scripts/check_hygiene.py``
(checks ``git ls-files``) or pass file paths explicitly (pre-commit).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FORBIDDEN_SUFFIXES = (".duckdb", ".parquet", ".jsonl", ".zst", ".pem")
MAX_BYTES = 500 * 1024
SIZE_EXEMPT_PREFIXES = ("reports/figures/",)


def problems(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/")
        if p == "data" or p.startswith("data/"):
            out.append(f"{p}: tracked file under data/")
        if p.lower().endswith(FORBIDDEN_SUFFIXES):
            out.append(f"{p}: forbidden data/key file type")
        f = Path(raw)
        if f.is_file() and not p.startswith(SIZE_EXEMPT_PREFIXES) and f.stat().st_size > MAX_BYTES:
            out.append(f"{p}: {f.stat().st_size / 1024:.0f} KB exceeds 500 KB")
    return out


def main(argv: list[str]) -> int:
    paths = (
        argv
        or subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    )
    found = problems(paths)
    for line in found:
        print(f"hygiene: {line}", file=sys.stderr)
    print(f"hygiene: checked {len(paths)} files, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
