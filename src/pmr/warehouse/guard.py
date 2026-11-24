"""Guard for warehouse builds on the shared PC (owner decision 2026-11-24, plan A).

A build runs one committed step at a time. While a step runs, a watcher thread checks every few
seconds:

* free memory: ``MemAvailable`` below ``min_avail_gb`` (the shared-PC floor, 30 GB);
* the paper service: its heartbeat file older than ``max_heartbeat_age_s`` means polling fell
  behind, and the build yields to it;
* the disk: a rise in any ext4 ``errors_count`` under ``/sys/fs/ext4`` (after the G: drive
  failure of 2026-11-20);
* writes: more than ``max_write_gb`` written by this process (a run should write 0.5–2 GB).

On any of these the watcher interrupts the running DuckDB query, so the step's transaction
rolls back, and the build raises :class:`BuildStopped`. Checks that cannot run on this platform
(no ``/proc``, no ``/sys/fs/ext4``) are skipped.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Reasons after which later runs must not start until a person clears the stop file.
STICKY = ("disk_error", "write_cap")


class BuildStopped(Exception):
    """The guard stopped the build; ``sticky`` means later runs must not start by themselves."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sticky = reason.split(":", 1)[0] in STICKY


class _Interruptible(Protocol):
    def interrupt(self) -> None: ...


def mem_available_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576
    except OSError:
        return None
    return None


def ext4_errors() -> dict[str, int]:
    out: dict[str, int] = {}
    root = Path("/sys/fs/ext4")
    if not root.is_dir():
        return out
    for dev in root.iterdir():
        f = dev / "errors_count"
        try:
            out[dev.name] = int(f.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
    return out


def written_bytes(path: Path | None = None) -> int | None:
    """Bytes written so far: by this process (``/proc/self/io``) or, where the kernel has no
    per-process I/O accounting (the WSL kernel), by the whole block device that holds ``path``
    (``/proc/diskstats``). The device count includes other programs' writes, so the cap is
    then stricter, never looser."""
    try:
        with open("/proc/self/io", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except OSError:
        pass
    if path is None:
        return None
    try:
        major, minor = getattr(os, "major", None), getattr(os, "minor", None)
        if major is None or minor is None:  # not on POSIX
            return None
        st = os.stat(path)
        dev = f"{major(st.st_dev)}:{minor(st.st_dev)}"
        name = Path(f"/sys/dev/block/{dev}").resolve().name
        with open("/proc/diskstats", encoding="ascii") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 9 and f[2] == name:
                    return int(f[9]) * 512
    except (OSError, ValueError):
        return None
    return None


@dataclass
class Guard:
    """Thresholds plus the baseline taken when the guard is created."""

    heartbeat: Path | None = None
    data_dir: Path | None = None  # the device measured for the write cap when needed
    max_heartbeat_age_s: float = 120.0
    min_avail_gb: float = 30.0
    max_write_gb: float = 4.0
    interval_s: float = 5.0
    reason: str | None = None
    _errors0: dict[str, int] = field(default_factory=lambda: ext4_errors())
    _written0: int | None = None

    def __post_init__(self) -> None:
        self._written0 = written_bytes(self.data_dir)

    def check(self) -> str | None:
        """The first reason to stop, or None."""
        avail = mem_available_gb()
        if avail is not None and avail < self.min_avail_gb:
            return f"memory: MemAvailable {avail:.1f} GB < {self.min_avail_gb:g} GB"
        for dev, n in ext4_errors().items():
            if n > self._errors0.get(dev, 0):
                return f"disk_error: ext4 {dev} errors_count {self._errors0.get(dev, 0)} -> {n}"
        w = written_bytes(self.data_dir)
        if w is not None and self._written0 is not None:
            gb = (w - self._written0) / 1e9
            if gb > self.max_write_gb:
                return f"write_cap: {gb:.1f} GB written > {self.max_write_gb:g} GB"
        if self.heartbeat is not None:
            try:
                age = time.time() - self.heartbeat.stat().st_mtime
            except OSError:
                return f"paper_behind: no heartbeat at {self.heartbeat}"
            if age > self.max_heartbeat_age_s:
                return f"paper_behind: heartbeat {age:.0f} s old > {self.max_heartbeat_age_s:g} s"
        return None

    def written_gb(self) -> float | None:
        """GB written by this process since the guard was created."""
        w = written_bytes(self.data_dir)
        return None if w is None or self._written0 is None else (w - self._written0) / 1e9

    def raise_if_stopped(self) -> None:
        """Raise :class:`BuildStopped` when a check fails (before a step starts)."""
        r = self.reason or self.check()
        if r:
            self.reason = r
            raise BuildStopped(r)

    @contextmanager
    def watch(self, con: _Interruptible) -> Iterator[None]:
        """Interrupt ``con``'s running query as soon as a check fails."""
        done = threading.Event()

        def loop() -> None:
            while not done.wait(self.interval_s):
                r = self.check()
                if r:
                    self.reason = r
                    con.interrupt()
                    return

        t = threading.Thread(target=loop, name="warehouse-guard", daemon=True)
        t.start()
        try:
            yield
        finally:
            done.set()
            t.join(timeout=self.interval_s + 1)


def default_heartbeat(data_dir: Path) -> Path | None:
    """The paper service's heartbeat file, when the service has one."""
    p = Path(data_dir) / "paper" / "heartbeat.json"
    return p if p.exists() else None


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default
