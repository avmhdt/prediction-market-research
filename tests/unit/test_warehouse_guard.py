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
