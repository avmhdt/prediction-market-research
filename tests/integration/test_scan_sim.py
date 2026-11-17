"""``pmr arb scan`` and ``pmr mm sim`` on a small warehouse built from the real DDL."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from pmr import cli
from pmr.analysis.arb import ArbConfig
from pmr.analysis.scan import scan
from pmr.fees.table import FeeTable
from pmr.sim.mm import MMParams
from pmr.sim.run import paper_symbols, run
from pmr.warehouse.build import DDL, VIEWS
from tests.conftest import REPO

NS = 10**9
T = 1_795_000_000


def make_warehouse(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(DDL.read_text(encoding="utf-8"))
    con.execute(VIEWS.read_text(encoding="utf-8"))

    def market(venue: str, symbol: str, no: str | None, mid: str, result: str | None) -> None:
        con.execute(
            "INSERT INTO markets (venue, symbol, market_id, category, status, result, "
            "settlement_value, contract_type, is_play_money, is_forecast, disputed, "
            "rules_changed, valid_from, ingested_at, close_ts, resolved_ts, no_symbol) "
            "VALUES (?, ?, ?, 'politics', ?, ?, ?, 'binary_event', false, false, false, false, "
            "0, 0, ?, ?, ?)",
            [
                venue,
                symbol,
                mid,
                "resolved" if result else "open",
                result,
                1.0 if result == "YES" else None,
                (T + 30 * 86400) * NS,
                (T + 3000) * NS if result else None,
                no,
            ],
        )

    market("polymarket", "Y", "N", "0xc", None)
    market("kalshi", "KX", None, "KX", "YES")
    rows = []
    for seq in (1, 2):
        ts = (T + 60 * seq) * NS
        # YES asks 0.45, NO-token asks 0.50: YES + NO = 0.95 < 1 on both snapshots
        rows += [
            (ts, "polymarket", "Y", seq, 1, 0.40, 100.0),
            (ts, "polymarket", "Y", seq, -1, 0.45, 100.0),
            (ts, "polymarket", "N", seq, 1, 0.50, 100.0),
            (ts, "polymarket", "N", seq, -1, 0.50, 100.0),
            (ts, "kalshi", "KX", seq, 1, 0.48, 50.0),
            (ts, "kalshi", "KX", seq, -1, 0.52, 50.0),
        ]
    con.executemany("INSERT INTO book_l2 VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, true)", rows)
    con.executemany(
        "INSERT INTO trades VALUES (?, ?, 'kalshi', 'KX', ?, ?, ?, ?, 'KX', 'api', ?)",
        [
            ((T + 90) * NS, (T + 90) * NS, "t1", 0.45, 30.0, -1, 13.5),
            ((T + 150) * NS, (T + 150) * NS, "t2", 0.56, 30.0, 1, 16.8),
        ],
    )
    con.close()


def cfg() -> ArbConfig:
    return ArbConfig(fees=FeeTable.load(REPO / "config" / "fees.yaml"), r_annual=0.04)


def test_scan_historical_live_and_pairs(tmp_path: Path) -> None:
    wh = tmp_path / "w.duckdb"
    make_warehouse(wh)
    pairs = tmp_path / "map.yaml"
    pairs.write_text(
        "pairs:\n"
        "  - {a: {venue: kalshi, market_id: KX}, b: {venue: polymarket, market_id: '0xc'},\n"
        "     orientation: 1, status: confirmed, material_diffs: [deadline]}\n"
        "  - {a: {venue: kalshi, market_id: KX}, b: {venue: polymarket, market_id: '0xc'},\n"
        "     orientation: 1, status: rejected}\n",
        encoding="utf-8",
    )
    hist = scan(wh, cfg(), pairs_path=pairs)
    yes_no = hist.rows.filter(hist.rows["kind"] == "yes_no")
    assert yes_no.height == 2 and yes_no["cost"].to_list() == [pytest.approx(0.95)] * 2
    assert hist.rows.filter(hist.rows["kind"] == "cross_venue").height >= 1
    live = (
        scan(wh, cfg(), live=True, pairs_path=tmp_path / "none.yaml")
        if False
        else scan(wh, cfg(), live=True, pairs_path=pairs)
    )
    assert live.rows.filter(live.rows["kind"] == "yes_no").height == 1
    empty = scan(wh, cfg(), since=date(2030, 1, 1), pairs_path=pairs)
    assert empty.rows.is_empty()


def test_mm_run_and_paper_symbols(tmp_path: Path) -> None:
    wh = tmp_path / "w.duckdb"
    make_warehouse(wh)
    params = MMParams(
        half_spread=0.02, skew_gamma=0.0, q_max=100, capital_max=1000, ema_halflife_s=300
    )
    df = run(wh, params, [("kalshi", "KX"), ("kalshi", "MISSING")])
    assert df.height == 1 and df["n_fills"][0] >= 1
    row = df.row(0, named=True)
    assert (
        abs(
            row["total"]
            - (row["spread_capture"] + row["inventory_mtm"] + row["settlement"] - row["fees"])
        )
        < 1e-9
    )
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "market_set.json").write_text(
        json.dumps(
            {"members": {"kalshi:KX": {}}, "history": [{"event": "added", "key": "polymarket:Y"}]}
        )
    )
    assert paper_symbols(tmp_path) == [("kalshi", "KX"), ("polymarket", "Y")]
    assert paper_symbols(tmp_path / "nothing") == []


def test_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wh = tmp_path / "data" / "warehouse.duckdb"
    wh.parent.mkdir(parents=True)
    make_warehouse(wh)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["arb", "scan"]).exit_code == 2
    assert runner.invoke(cli.app, ["mm", "sim"]).exit_code == 2
    assert runner.invoke(cli.app, ["mm", "sim", "--set", "other"]).exit_code == 2
    monkeypatch.chdir(tmp_path)
    for name in ("config",):
        (tmp_path / name).mkdir()
        for f in ("pmr.yaml", "fees.yaml", "categories.yaml", "contract_map.yaml"):
            (tmp_path / name / f).write_text(
                (REPO / "config" / f).read_text(encoding="utf-8"), encoding="utf-8"
            )
    res = runner.invoke(cli.app, ["arb", "scan", "--historical"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "results" / "arb_scan_historical_summary.csv").exists()
    res = runner.invoke(cli.app, ["mm", "sim", "--market", "kalshi:KX"])
    assert res.exit_code == 0, res.output
    assert "kalshi,KX" in (tmp_path / "results" / "mm_pnl_decomposition.csv").read_text()
    log = (tmp_path / "results" / "experiment_log.csv").read_text()
    assert "arb scan --historical" in log and "mm sim" in log
