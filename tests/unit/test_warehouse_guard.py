"""The warehouse build guard (owner decision 2026-11-24): each check, and the interrupt."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from pmr.warehouse import guard as g


def test_paper_heartbeat_age(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    gd = g.Guard(heartbeat=hb, max_heartbeat_age_s=120)
    assert gd.check() is None
    old = time.time() - 300
    os.utime(hb, (old, old))
    r = gd.check()
    assert r is not None and r.startswith("paper_behind")
    assert g.Guard(heartbeat=tmp_path / "none").check() is not None


def test_memory_disk_and_write_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "mem_available_gb", lambda: 29.5)
    r = g.Guard().check()
    assert r is not None and r.startswith("memory")
    assert not g.BuildStopped(r).sticky

    monkeypatch.setattr(g, "mem_available_gb", lambda: 50.0)
    monkeypatch.setattr(g, "ext4_errors", lambda: {"sdd": 0})
    gd = g.Guard()
    assert gd.check() is None
    monkeypatch.setattr(g, "ext4_errors", lambda: {"sdd": 1})
    r = gd.check()
    assert r is not None and r.startswith("disk_error") and g.BuildStopped(r).sticky

    monkeypatch.setattr(g, "ext4_errors", lambda: {})
    monkeypatch.setattr(g, "written_bytes", lambda _p=None: 0)
    gd = g.Guard(max_write_gb=4)
    monkeypatch.setattr(g, "written_bytes", lambda _p=None: int(5e9))
    r = gd.check()
    assert r is not None and r.startswith("write_cap") and g.BuildStopped(r).sticky


def test_watch_interrupts_and_raise_if_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = threading.Event()

    class Con:
        def interrupt(self) -> None:
            hit.set()

    monkeypatch.setattr(g, "mem_available_gb", lambda: 10.0)
    gd = g.Guard(interval_s=0.01)
    with gd.watch(Con()):
        assert hit.wait(2)
    assert gd.reason is not None and gd.reason.startswith("memory")
    with pytest.raises(g.BuildStopped):
        gd.raise_if_stopped()


def test_fd_limit_is_sticky_and_not_a_disk_error() -> None:
    e = g.BuildStopped("fd_limit: IO Error: Cannot open file x: Too many open files")
    assert e.sticky and not e.reason.startswith("disk_error")


def test_reason_set_during_commit_still_stops(tmp_path: Path) -> None:
    """A reason set while a step commits (a COMMIT cannot be interrupted) stops the run after
    that step: the step stays committed and the build raises."""
    from pmr.normalize import categories
    from pmr.warehouse.build import build
    from tests.conftest import REPO

    class Late(g.Guard):
        def raise_if_stopped(self) -> None:  # the reason appears only after the step began
            return None

    cats = categories.load(str(REPO / "config" / "categories.yaml"))
    gd = Late(interval_s=3600)
    gd.reason = "write_cap: 5.0 GB written > 4 GB"
    with pytest.raises(g.BuildStopped, match="write_cap"):
        build(tmp_path, tmp_path / "w.duckdb", cats, threads=1, max_steps=1, guard=gd)
    assert (tmp_path / "w.duckdb.tmp").exists()
