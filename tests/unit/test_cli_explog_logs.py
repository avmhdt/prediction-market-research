from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from pmr import cli, explog, logs

REPO = Path(__file__).resolve().parents[2]

STUBS = [
    ["ingest", "kalshi"],
    ["ingest", "polymarket"],
    ["ingest", "manifold"],
    ["ingest", "metaculus"],
    ["ingest", "fred"],
    ["normalize"],
    ["warehouse", "build"],
    ["warehouse", "migrate"],
    ["warehouse", "stats"],
    ["warehouse", "checksum"],
    ["inventory"],
    ["match", "candidates"],
    ["match", "export-review"],
    ["match", "import-review"],
    ["match", "eval"],
    ["match", "rules"],
    ["fees", "check"],
    ["calib", "run"],
    ["flb", "run"],
    ["arb", "scan"],
    ["arb", "catalogue"],
    ["mm", "sim"],
    ["record", "books"],
    ["paper", "run"],
    ["paper", "status"],
    ["paper", "compare"],
    ["dataset", "export"],
    ["report", "all"],
]


@pytest.mark.parametrize("args", STUBS, ids=lambda a: " ".join(a))
def test_unbuilt_commands_exit_2_with_milestone(args: list[str]) -> None:
    res = CliRunner().invoke(cli.app, [*args, "--config", "config/pmr.yaml"])
    assert res.exit_code == 2
    assert "not implemented yet" in res.output


def test_help_lists_every_group() -> None:
    res = CliRunner().invoke(cli.app, ["--help"])
    assert res.exit_code == 0
    for name in ("smoke", "ingest", "warehouse", "match", "fees", "calib", "flb", "arb", "mm"):
        assert name in res.output


def test_explog_append(tmp_path: Path) -> None:
    p = tmp_path / "results" / "experiment_log.csv"
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    explog.append(
        "calib run", params={"b": 2, "a": 1}, n=10, metrics={"brier": 0.2}, path=p, now=now
    )
    explog.append("flb run", params={}, n=0, metrics={}, path=p, now=now, cfg_hash="abc")
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert [r["command"] for r in rows] == ["calib run", "flb run"]
    assert rows[0]["ts_utc"] == "2026-11-09T12:00:00Z"
    assert rows[0]["params_json"] == '{"a":1,"b":2}'
    assert json.loads(rows[0]["metrics_json"]) == {"brier": 0.2}
    assert rows[0]["git_dirty"] in {"true", "false"}
    assert tuple(rows[0]) == explog.COLUMNS


def test_git_state_outside_repo(tmp_path: Path) -> None:
    assert explog.git_state(tmp_path / "nope") == ("unknown", True)


def test_config_hash_stable(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    a.write_text("x: 1\n")
    h1 = explog.config_hash(a)
    assert h1 == explog.config_hash(a) and len(h1) == 16
    a.write_text("x: 2\n")
    assert explog.config_hash(a) != h1


def test_logs_redact_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    logs.configure("INFO")
    structlog.get_logger("t").info("evt", api_key="k", metaculus_api_token="t", limit=3)
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["api_key"] == "***" and line["metaculus_api_token"] == "***"
    assert line["limit"] == 3 and line["level"] == "info"
    logs.configure("WARNING", json=False)
    structlog.get_logger("t").info("hidden")
    assert "hidden" not in capsys.readouterr().err
