"""``pmr`` command line (SPEC section 10).

Every command accepts ``--config PATH`` (default ``config/pmr.yaml``) and ``--log-level``.
Commands whose milestone has not been built yet exit with code 2 and name the milestone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pmr import logs
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


@ingest_app.command("kalshi")
def ingest_kalshi(
    what: Annotated[str, typer.Option("--what")] = "markets",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Kalshi live and historical, routed by /historical/cutoff."""
    _not_yet("ingest kalshi", "M2")


@ingest_app.command("polymarket")
def ingest_polymarket(
    source: Annotated[str, typer.Option("--source")] = "gamma",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Polymarket metadata, books, price history, trades and on-chain fills."""
    _not_yet("ingest polymarket", "M2")


@ingest_app.command("manifold")
def ingest_manifold(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Resolved binary Manifold markets and their bets."""
    _not_yet("ingest manifold", "M2")


@ingest_app.command("metaculus")
def ingest_metaculus(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Metaculus binary questions and community prediction histories."""
    _not_yet("ingest metaculus", "M2")


@ingest_app.command("fred")
def ingest_fred(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """FRED DTB3 series."""
    _not_yet("ingest fred", "M2")


# ---------------------------------------------------------------- normalize, warehouse


@app.command()
def normalize(
    venue: Annotated[str | None, typer.Option("--venue")] = None,
    rebuild: Annotated[bool, typer.Option("--rebuild")] = False,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Raw archive to interim Parquet in the unified schema."""
    _not_yet("normalize", "M2")


warehouse_app = typer.Typer(help="DuckDB warehouse (M2).", no_args_is_help=True)
app.add_typer(warehouse_app, name="warehouse")


def _warehouse_cmd(name: str) -> None:
    @warehouse_app.command(name)
    def _cmd(
        path: Annotated[Path | None, typer.Option("--path")] = None,
        config: ConfigOpt = DEFAULT_CONFIG_PATH,
        log_level: LogOpt = None,
    ) -> None:
        _not_yet(f"warehouse {name}", "M2")

    _cmd.__doc__ = f"Warehouse {name}."


for _name in ("build", "migrate", "stats", "checksum"):
    _warehouse_cmd(_name)


@app.command()
def inventory(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """Write reports/data_inventory.md."""
    _not_yet("inventory", "M2")


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

record_app = typer.Typer(help="Book recorder (M4).", no_args_is_help=True)
app.add_typer(record_app, name="record")


@record_app.command("books")
def record_books(
    duration: Annotated[str | None, typer.Option("--duration")] = None,
    paper_config: Annotated[Path, typer.Option("--paper-config")] = Path("config/paper.yaml"),
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Long-running poller for the paper-trading market set."""
    _not_yet("record books", "M4")


paper_app = typer.Typer(help="Paper trader (M4, M6). Paper only.", no_args_is_help=True)
app.add_typer(paper_app, name="paper")


@paper_app.command("run")
def paper_run(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """Run the paper strategies forward on recorder data."""
    _not_yet("paper run", "M4")


@paper_app.command("status")
def paper_status(config: ConfigOpt = DEFAULT_CONFIG_PATH, log_level: LogOpt = None) -> None:
    """Uptime, gaps and disk flag of the recorder and paper trader."""
    _not_yet("paper status", "M4")


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
