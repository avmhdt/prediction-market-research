"""``pmr`` command line (SPEC section 10).

Every command accepts ``--config PATH`` (default ``config/pmr.yaml``) and ``--log-level``.
Commands whose milestone has not been built yet exit with code 2 and name the milestone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from pmr import logs
from pmr.collectors.base import CollectorError, IngestStats
from pmr.settings import DEFAULT_CONFIG_PATH, PmrConfig, Settings, load_config

app = typer.Typer(
    name="pmr",
    help="Prediction market research: collectors, warehouse, calibration, FLB, arb, paper.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = Annotated[Path, typer.Option("--config", help="Main run config (YAML).")]
LogOpt = Annotated[
    str | None, typer.Option("--log-level", help="DEBUG | INFO | WARNING (default: .env).")
]


def _setup(config: Path, log_level: str | None) -> tuple[Settings, PmrConfig]:
    settings = Settings()
    logs.configure(log_level or settings.pmr_log_level)
    return settings, load_config(config, settings)


def _not_yet(command: str, milestone: str) -> NoReturn:
    typer.echo(f"pmr {command}: not implemented yet (arrives in {milestone}).", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------------------- smoke


@app.command()
def smoke(
    venue: Annotated[
        list[str] | None,
        typer.Option("--venue", help="Limit to sources starting with this name (repeatable)."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Also write the report to this file.")
    ] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """One request per venue endpoint family; exit code 1 if a required source fails."""
    from pmr import smoke as smoke_mod

    settings, cfg = _setup(config, log_level or "WARNING")
    results = smoke_mod.run(settings, cfg, sources=venue)
    report = smoke_mod.format_report(results, owner_country=settings.owner_country)
    typer.echo(report)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8", newline="\n")
    raise typer.Exit(code=smoke_mod.exit_code(results))


# ------------------------------------------------------------------------------ ingest

ingest_app = typer.Typer(help="Collect raw data from a venue (M2).", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")


def _window(cfg: PmrConfig, since: str | None, until: str | None) -> tuple[date, date | None]:
    lo = date.fromisoformat(since) if since else cfg.study_window.from_
    hi_cfg = cfg.study_window.to
    hi = date.fromisoformat(until) if until else (None if hi_cfg == "latest" else hi_cfg)
    return lo, hi


def _run_ingest(label: str, coro: Coroutine[Any, Any, IngestStats]) -> None:
    try:
        stats = asyncio.run(coro)
    except CollectorError as exc:
        typer.echo(f"pmr ingest {label}: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(f"pmr ingest {label}: {stats.as_dict()}")


@ingest_app.command("kalshi")
def ingest_kalshi(
    what: Annotated[
        str, typer.Option("--what", help="series|events|markets|historical|trades|candles|books")
    ] = "markets",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Kalshi live and historical, routed by /historical/cutoff (disabled until allowed)."""
    from pmr.collectors.run import ingest_kalshi as run

    settings, cfg = _setup(config, log_level)
    lo, hi = _window(cfg, since, until)
    _run_ingest(
        f"kalshi --what {what}",
        run(settings, cfg, settings.data_dir(), what, since=lo, until=hi, status=status),
    )


@ingest_app.command("polymarket")
def ingest_polymarket(
    source: Annotated[str, typer.Option("--source", help="gamma|data|clob|onchain")] = "gamma",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Polymarket metadata, price history, trades and on-chain fills."""
    from pmr.collectors.run import ingest_polymarket as run

    settings, cfg = _setup(config, log_level)
    lo, hi = _window(cfg, since, until)
    _run_ingest(
        f"polymarket --source {source}",
        run(settings, cfg, settings.data_dir(), source, since=lo, until=hi),
    )


@ingest_app.command("manifold")
def ingest_manifold(
    what: Annotated[str, typer.Option("--what", help="markets|details|bets")] = "markets",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Resolved binary Manifold markets, their full records and their bets."""
    from pmr.collectors.run import ingest_manifold as run

    settings, cfg = _setup(config, log_level)
    lo, hi = _window(cfg, since, until)
    _run_ingest(
        f"manifold --what {what}",
        run(settings, cfg, settings.data_dir(), what, since=lo, until=hi),
    )


@ingest_app.command("metaculus")
def ingest_metaculus(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Metaculus binary questions and community prediction histories (needs the token)."""
    from pmr.collectors.run import ingest_metaculus as run

    settings, cfg = _setup(config, log_level)
    lo, hi = _window(cfg, since, until)
    _run_ingest("metaculus", run(settings, cfg, settings.data_dir(), since=lo, until=hi))


@ingest_app.command("fred")
def ingest_fred(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """FRED DTB3 series."""
    from pmr.collectors.run import ingest_fred as run

    settings, cfg = _setup(config, log_level)
    _run_ingest("fred", run(settings, cfg, settings.data_dir()))


# ---------------------------------------------------------------- normalize, warehouse


@app.command()
def normalize(
    venue: Annotated[str | None, typer.Option("--venue")] = None,
    rebuild: Annotated[bool, typer.Option("--rebuild")] = False,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Raw archive to interim Parquet in the unified schema."""
    from pmr.normalize import categories
    from pmr.normalize.run import run

    settings, _ = _setup(config, log_level)
    st = run(settings.data_dir(), categories.load(), venue=venue, rebuild=rebuild)
    typer.echo(f"pmr normalize: parts={st.parts} skipped={st.skipped} rows={st.rows}")


warehouse_app = typer.Typer(help="DuckDB warehouse.", no_args_is_help=True)
app.add_typer(warehouse_app, name="warehouse")

PathOpt = Annotated[Path | None, typer.Option("--path", help="Warehouse file (default: settings).")]


def _wh_path(settings: Settings, path: Path | None) -> Path:
    return path or settings.warehouse_path()


@warehouse_app.command("build")
def warehouse_build(
    path: PathOpt = None, config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None
) -> None:
    """Rebuild the warehouse from interim Parquet."""
    from pmr.normalize import categories
    from pmr.warehouse.build import build

    settings, _ = _setup(config, log_level)
    res = build(settings.data_dir(), _wh_path(settings, path), categories.load())
    typer.echo(f"pmr warehouse build: {res.path} {res.rows}")


@warehouse_app.command("migrate")
def warehouse_migrate(
    path: PathOpt = None, config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None
) -> None:
    """Apply pending schema migrations."""
    from pmr.warehouse.build import migrate

    settings, _ = _setup(config, log_level)
    typer.echo(f"pmr warehouse migrate: applied {migrate(_wh_path(settings, path))}")


@warehouse_app.command("stats")
def warehouse_stats(
    path: PathOpt = None, config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None
) -> None:
    """Rows per table and venue."""
    from pmr.warehouse.build import stats

    settings, _ = _setup(config, log_level)
    for table, counts in stats(_wh_path(settings, path)).items():
        typer.echo(f"{table}: {counts}")


@warehouse_app.command("checksum")
def warehouse_checksum(
    path: PathOpt = None, config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None
) -> None:
    """Per-table checksums (two rebuilds must agree, AC-W-04)."""
    from pmr.warehouse.build import checksums

    settings, _ = _setup(config, log_level)
    for table, value in checksums(_wh_path(settings, path)).items():
        typer.echo(f"{table} {value}")


@app.command()
def inventory(
    out: Annotated[Path, typer.Option("--out")] = Path("reports/data_inventory.md"),
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Write reports/data_inventory.md."""
    from pmr.warehouse.inventory import inventory as run

    settings, cfg = _setup(config, log_level)
    lo, hi = _window(cfg, None, None)
    lf = cfg.calibration.liquidity_filter
    res = run(
        settings.warehouse_path(),
        settings.data_dir(),
        since=lo,
        until=hi,
        min_trades=lf.min_trades,
        min_notional=lf.min_notional,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(res.markdown, encoding="utf-8", newline="\n")
    typer.echo(
        f"pmr inventory: eligible={res.eligible_by_venue} total={res.eligible_total} -> {out}"
    )


# ------------------------------------------------------------------------------- match

match_app = typer.Typer(help="Cross-venue contract matcher (M3).", no_args_is_help=True)
app.add_typer(match_app, name="match")


def _match_cmd(name: str) -> None:
    @match_app.command(name)
    def _cmd(
        venues: Annotated[str | None, typer.Option("--venues")] = None,
        n: Annotated[int | None, typer.Option("--n")] = None,
        file: Annotated[Path | None, typer.Option("--file")] = None,
        config: ConfigOpt = DEFAULT_CONFIG_PATH,
        log_level: LogOpt = None,
    ) -> None:
        _not_yet(f"match {name}", "M3")

    _cmd.__doc__ = f"Matcher: {name}."


for _name in ("candidates", "export-review", "import-review", "eval", "rules"):
    _match_cmd(_name)


fees_app = typer.Typer(help="Fee schedules (M3).", no_args_is_help=True)
app.add_typer(fees_app, name="fees")


@fees_app.command("check")
def fees_check(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """Validate fees.yaml and run the worked examples."""
    _not_yet("fees check", "M3")


# ----------------------------------------------------------------------- calib and flb

calib_app = typer.Typer(help="Calibration (M5).", no_args_is_help=True)
app.add_typer(calib_app, name="calib")


@calib_app.command("run")
def calib_run(
    horizons: Annotated[str, typer.Option("--horizons")] = "1h,1d,7d,30d",
    by: Annotated[str, typer.Option("--by")] = "venue,category,bucket,ttx",
    unfiltered: Annotated[bool, typer.Option("--unfiltered")] = False,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Reliability tables, Brier decomposition and clustered bootstrap CIs."""
    _not_yet("calib run", "M5")


flb_app = typer.Typer(help="Favorite-longshot bias (M5).", no_args_is_help=True)
app.add_typer(flb_app, name="flb")


@flb_app.command("run")
def flb_run(
    net_of_fees: Annotated[bool, typer.Option("--net-of-fees/--gross")] = True,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """FLB GLM and return regressions, gross or net of fees."""
    _not_yet("flb run", "M5")


# --------------------------------------------------------------------------- arb and mm

arb_app = typer.Typer(
    help="Arbitrage scanner and mismatch catalogue (M4, M6).", no_args_is_help=True
)
app.add_typer(arb_app, name="arb")


@arb_app.command("scan")
def arb_scan(
    historical: Annotated[bool, typer.Option("--historical")] = False,
    live: Annotated[bool, typer.Option("--live")] = False,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Cross-venue and intra-venue scan net of fees, lockup and resolution haircut."""
    _not_yet("arb scan", "M4")


@arb_app.command("catalogue")
def arb_catalogue(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """Build the mismatch catalogue."""
    _not_yet("arb catalogue", "M6")


mm_app = typer.Typer(help="Market-making simulator (M4).", no_args_is_help=True)
app.add_typer(mm_app, name="mm")


@mm_app.command("sim")
def mm_sim(
    market: Annotated[str | None, typer.Option("--market", help="VENUE:SYMBOL")] = None,
    set_: Annotated[str | None, typer.Option("--set")] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Simulate market making on recorded books and trades."""
    _not_yet("mm sim", "M4")


# ----------------------------------------------------------------------- record, paper

record_app = typer.Typer(help="Book recorder.", no_args_is_help=True)
app.add_typer(record_app, name="record")

PaperCfgOpt = Annotated[Path, typer.Option("--paper-config")]


def parse_duration(text: str | None) -> float | None:
    """``28d``, ``72h``, ``10m`` or ``30s`` to seconds (None for forever)."""
    if not text:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    if text[-1] not in units:
        raise typer.BadParameter("use a number followed by d, h, m or s")
    return float(text[:-1]) * units[text[-1]]


def _recorder(config: Path, paper_config: Path, log_level: str | None, *, paper: bool) -> Any:
    from pmr.fees.table import FeeTable
    from pmr.normalize import categories
    from pmr.paper.config import load_paper_config
    from pmr.paper.recorder import Recorder, config_hash

    settings, cfg = _setup(config, log_level)
    pcfg = load_paper_config(paper_config, settings)
    fees_path = Path("config/fees.yaml")
    return Recorder(
        settings,
        cfg,
        pcfg,
        settings.data_dir(),
        categories.load(),
        FeeTable.load(fees_path),
        paper=paper,
        config_hash=config_hash(config, paper_config, fees_path),
    )


@record_app.command("books")
def record_books(
    duration: Annotated[str | None, typer.Option("--duration")] = None,
    paper_config: PaperCfgOpt = Path("config/paper.yaml"),
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Poll books and trades for the market set (no paper strategies)."""
    rec = _recorder(config, paper_config, log_level, paper=False)
    asyncio.run(rec.run(parse_duration(duration)))


paper_app = typer.Typer(
    help="Paper trader. Paper only: no order is ever sent.", no_args_is_help=True
)
app.add_typer(paper_app, name="paper")


@paper_app.command("run")
def paper_run(
    duration: Annotated[str | None, typer.Option("--duration")] = None,
    paper_config: PaperCfgOpt = Path("config/paper.yaml"),
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Record books and run the paper strategies on every poll (the long-running service)."""
    rec = _recorder(config, paper_config, log_level, paper=True)
    asyncio.run(rec.run(parse_duration(duration)))


@paper_app.command("status")
def paper_status(
    paper_config: PaperCfgOpt = Path("config/paper.yaml"),
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Uptime per venue, gaps and disk flag of the recorder and paper trader."""
    import json as _json

    from pmr.paper.config import load_paper_config
    from pmr.paper.recorder import status

    settings, _ = _setup(config, log_level)
    pcfg = load_paper_config(paper_config, settings)
    typer.echo(_json.dumps(status(settings.data_dir(), pcfg), indent=1))


@paper_app.command("compare")
def paper_compare(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Compare paper trading with the simulator over the same days."""
    _not_yet("paper compare", "M6")


# --------------------------------------------------------------------- dataset, report

dataset_app = typer.Typer(help="Derived dataset (M6).", no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("export")
def dataset_export(
    out: Annotated[Path, typer.Option("--out")] = Path("data/processed/dataset"),
    version: Annotated[str, typer.Option("--version")] = "1.0.0",
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Export derived aggregates for approved venues (refuses while D-05 is unset)."""
    _not_yet("dataset export", "M6")


report_app = typer.Typer(help="Reports (M7).", no_args_is_help=True)
app.add_typer(report_app, name="report")


@report_app.command("all")
def report_all(
    check: Annotated[bool, typer.Option("--check")] = False,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Regenerate every figure, table and summary number."""
    _not_yet("report all", "M7")


if __name__ == "__main__":
    app()
