"""Interim Parquet store: ``<data>/interim/<dataset>/venue=<venue>/date=YYYY-MM-DD/part-*.parquet``.

One interim file per raw part and dataset, named after the raw part, so normalizing the same
raw part again rewrites the same file with the same bytes (idempotent, NFR-04). The canonical
``symbol=`` partition level is dropped on purpose (SPEC section 7d): symbol stays a column.
"""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl

from pmr.normalize.schema import DATASETS


def interim_root(data_dir: Path) -> Path:
    """``<data>/interim``."""
    return Path(data_dir) / "interim"


def interim_path(data_dir: Path, dataset: str, venue: str, day: str, part_stem: str) -> Path:
    """Interim file for one raw part."""
    return (
        interim_root(data_dir) / dataset / f"venue={venue}" / f"date={day}" / f"{part_stem}.parquet"
    )


def write_interim(df: pl.DataFrame, path: Path) -> None:
    """Write ``df`` (zstd Parquet, no statistics timestamps) atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.write_parquet(tmp, compression="zstd", statistics=False)
    os.replace(tmp, path)


def interim_files(data_dir: Path, dataset: str, venue: str | None = None) -> list[Path]:
    """Sorted interim files of a dataset."""
    root = interim_root(data_dir) / dataset
    return sorted(root.glob(f"venue={venue or '*'}/date=*/*.parquet"))


def read_interim(data_dir: Path, dataset: str, venue: str | None = None) -> pl.DataFrame:
    """All interim rows of a dataset (empty frame with the schema when there are none)."""
    files = interim_files(data_dir, dataset, venue)
    if not files:
        return pl.DataFrame(schema=DATASETS[dataset])
    return pl.concat([pl.read_parquet(f) for f in files], how="vertical")


def current_markets(data_dir: Path, venue: str | None = None) -> pl.DataFrame:
    """Latest normalized version of each market (by ``valid_from``) for ``venue``.

    A full record (with rules text, for example Manifold's market detail) wins over any lite
    record of the same market; among records of the same kind the latest wins.
    """
    m = read_interim(data_dir, "markets", venue)
    if m.is_empty():
        return m
    return (
        m.with_columns(pl.col("rules_text").is_not_null().alias("_full"))
        .sort(["venue", "symbol", "_full", "valid_from"])
        .unique(subset=["venue", "symbol"], keep="last", maintain_order=True)
        .drop("_full")
    )
