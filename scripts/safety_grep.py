"""AC-W-10 safety grep: paper only, no secrets.

Fails if any tracked Python file imports py_clob_client, or if private-key handling
(``load_pem_private_key``, ``-----BEGIN ... PRIVATE KEY-----``) appears outside
``src/pmr/http/kalshi_auth.py`` and the tests that generate throwaway keys.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CLOB_IMPORT = re.compile(r"^\s*(from|import)\s+py_clob_client\b", re.MULTILINE)
KEY_HANDLING = re.compile(r"load_pem_private_key|-----BEGIN [A-Z ]*PRIVATE KEY-----")
KEY_ALLOWED = {"src/pmr/http/kalshi_auth.py", "scripts/safety_grep.py"}
KEY_ALLOWED_PREFIXES = ("tests/",)


def scan(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/")
        f = Path(raw)
        if not f.is_file() or f.suffix not in {".py", ".pyi", ".toml", ".yaml", ".yml", ".md"}:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix in {".py", ".pyi"} and CLOB_IMPORT.search(text):
            out.append(f"{p}: imports py_clob_client (trading client is forbidden)")
        if (
            KEY_HANDLING.search(text)
            and p not in KEY_ALLOWED
            and not p.startswith(KEY_ALLOWED_PREFIXES)
        ):
            out.append(f"{p}: private-key handling outside kalshi_auth.py")
    return out


def main(argv: list[str]) -> int:
    paths = (
        argv
        or subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    )
    found = scan(paths)
    for line in found:
        print(f"safety-grep: {line}", file=sys.stderr)
    print(f"safety-grep: scanned {len(paths)} files, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
