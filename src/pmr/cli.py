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
    source: Annotated[
        str,
        typer.Option(
            "--source", help="onchain-markets|onchain-fills (history, from Brazil); gamma|data|clob"
        ),
    ] = "gamma",
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


EXIT_YIELDED = 75  # the guard stopped the run (memory, paper polling); the next run may retry
EXIT_STOPPED = 74  # disk error or write cap: later runs are blocked until a person clears it


@warehouse_app.command("build")
def warehouse_build(
    path: PathOpt = None,
    max_steps: Annotated[
        int | None,
        typer.Option("--max-steps", min=1, help="Stop after this many new steps (resumable)."),
    ] = None,
    guard: Annotated[
        bool,
        typer.Option(
            "--guard",
            help="Stop on low memory, a disk error, a write cap or stalled paper polling.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    log_level: LogOpt = None,
) -> None:
    """Build the warehouse from interim Parquet (resumes a stopped build).

    With ``--guard`` a disk error or the write cap writes ``<data>/warehouse.STOP``; while that
    file exists guarded runs refuse to start."""
    from pmr.normalize import categories
    from pmr.warehouse import guard as guard_mod
    from pmr.warehouse.build import build

    settings, _ = _setup(config, log_level)
    data = settings.data_dir()
    stop_file = data / "warehouse.STOP"
    g = None
    if guard:
        if stop_file.exists():
            typer.echo(f"pmr warehouse build: refused, {stop_file} exists")
            raise typer.Exit(EXIT_STOPPED)
        g = guard_mod.Guard(
            heartbeat=guard_mod.default_heartbeat(data),
            data_dir=data,
            max_heartbeat_age_s=guard_mod.env_float("PMR_GUARD_HEARTBEAT_S", 120.0),
            min_avail_gb=guard_mod.env_float("PMR_GUARD_MIN_AVAIL_GB", 30.0),
            max_write_gb=guard_mod.env_float("PMR_GUARD_MAX_WRITE_GB", 4.0),
        )
    try:
        res = build(data, _wh_path(settings, path), categories.load(), max_steps=max_steps, guard=g)
    except guard_mod.BuildStopped as e:
        gb = g.written_gb() if g is not None else None
        typer.echo(f"pmr warehouse build: stopped ({e.reason}); written {gb} GB")
        if e.sticky:
            stop_file.write_text(e.reason + "\n", encoding="utf-8")
            raise typer.Exit(EXIT_STOPPED) from e
        raise typer.Exit(EXIT_YIELDED) from e
    if g is not None:
        gb = g.written_gb()
        typer.echo(f"pmr warehouse build: written {gb:.2f} GB" if gb is not None else "")
    if res.complete:
        typer.echo(f"pmr warehouse build: complete {res.path} {res.rows}")
    else:
        typer.echo(f"pmr warehouse build: ran {list(res.steps_run)}, {res.steps_left} steps left")


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
    from pmr import explog
    from pmr.analysis.arb import ArbConfig
    from pmr.analysis.scan import scan
    from pmr.fees.table import FeeTable
    from pmr.paper.recorder import latest_dtb3

    if historical == live:
        typer.echo("pmr arb scan: choose exactly one of --historical or --live", err=True)
        raise typer.Exit(code=2)
    settings, cfg = _setup(config, log_level)
    acfg = ArbConfig(
        fees=FeeTable.load(Path("config/fees.yaml")),
        r_annual=latest_dtb3(settings.data_dir()),
        collateral_yield=dict(cfg.arb.collateral_yield),
        settlement_lag_hours={
            "kalshi": cfg.venues.kalshi.settlement_lag_hours,
            "polymarket": cfg.venues.polymarket.settlement_lag_hours,
        },
        transfer_cost_per_contract=cfg.arb.transfer_cost_per_contract,
        divergence_prior=cfg.arb.divergence_prior,
    )
    lo = date.fromisoformat(since) if since else None
    hi = date.fromisoformat(until) if until else None
    res = scan(settings.warehouse_path(), acfg, since=lo, until=hi, live=live)
    mode = "live" if live else "historical"
    out_dir = settings.data_dir() / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = res.rows.height
    if n:
        res.rows.write_parquet(out_dir / f"arb_opportunities_{mode}.parquet")
    summary = summarize_arb(res.rows, res.snapshots_checked, mode)
    Path("results").mkdir(exist_ok=True)
    summary.write_csv(f"results/arb_scan_{mode}_summary.csv", float_precision=6)
    explog.append(
        f"arb scan --{mode}",
        params={"since": since, "until": until},
        n=res.snapshots_checked,
        metrics={"rows": n, "net_positive": int(summary["net_positive"].sum())},
        cfg_hash=explog.config_hash(config, Path("config/fees.yaml")),
    )
    typer.echo(f"pmr arb scan --{mode}: {res.snapshots_checked} snapshots, {n} rows")


def summarize_arb(rows: Any, snapshots: int, mode: str) -> Any:
    """Aggregate opportunity rows per kind (aggregates only are committed)."""
    import polars as pl

    if rows.is_empty():
        return pl.DataFrame(
            {
                "mode": [mode],
                "kind": ["all"],
                "snapshots_checked": [snapshots],
                "n": [0],
                "net_positive": [0],
                "median_gross_edge": [None],
                "median_net_edge": [None],
            },
            schema={
                "mode": pl.String,
                "kind": pl.String,
                "snapshots_checked": pl.Int64,
                "n": pl.Int64,
                "net_positive": pl.Int64,
                "median_gross_edge": pl.Float64,
                "median_net_edge": pl.Float64,
            },
        )
    return (
        rows.group_by("kind")
        .agg(
            pl.len().cast(pl.Int64).alias("n"),
            (pl.col("net_edge") > 0).sum().cast(pl.Int64).alias("net_positive"),
            pl.col("gross_edge").median().alias("median_gross_edge"),
            pl.col("net_edge").median().alias("median_net_edge"),
        )
        .with_columns(
            pl.lit(mode).alias("mode"), pl.lit(snapshots).cast(pl.Int64).alias("snapshots_checked")
        )
        .select(
            "mode",
            "kind",
            "snapshots_checked",
            "n",
            "net_positive",
            "median_gross_edge",
            "median_net_edge",
        )
        .sort("kind")
    )


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
    from pmr import explog
    from pmr.sim.mm import MMParams
    from pmr.sim.run import paper_symbols, run

    if (market is None) == (set_ is None):
        typer.echo("pmr mm sim: give exactly one of --market VENUE:SYMBOL or --set paper", err=True)
        raise typer.Exit(code=2)
    settings, cfg = _setup(config, log_level)
    if market:
        venue, _, symbol = market.partition(":")
        markets = [(venue, symbol)]
    elif set_ == "paper":
        markets = paper_symbols(settings.data_dir())
    else:
        typer.echo(f"pmr mm sim: unknown set {set_!r} (only 'paper')", err=True)
        raise typer.Exit(code=2)
    p = cfg.mm
    params = MMParams(
        half_spread=p.half_spread,
        skew_gamma=p.skew_gamma,
        q_max=p.q_max,
        capital_max=p.capital_max,
        ema_halflife_s=p.ema_halflife_s,
    )
    lo = date.fromisoformat(since) if since else None
    hi = date.fromisoformat(until) if until else None
    df = run(settings.warehouse_path(), params, markets, since=lo, until=hi)
    Path("results").mkdir(exist_ok=True)
    df.write_csv("results/mm_pnl_decomposition.csv", float_precision=4)
    explog.append(
        "mm sim",
        params={"market": market, "set": set_, "since": since, "until": until},
        n=df.height,
        metrics={
            "total": float(df["total"].sum()) if df.height else 0.0,
            "fills": int(df["n_fills"].sum()) if df.height else 0,
        },
        cfg_hash=explog.config_hash(config),
    )
    typer.echo(f"pmr mm sim: {df.height} markets -> results/mm_pnl_decomposition.csv")


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
