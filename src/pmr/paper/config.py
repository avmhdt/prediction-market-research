"""Validated ``config/paper.yaml``."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from pmr.settings import Settings, load_yaml


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketsCfg(_M):
    from_pairs: str = "confirmed"
    extra: list[str] = Field(default_factory=list)
    max_markets: int = Field(default=200, ge=1)
    per_venue: dict[str, int] = Field(default_factory=lambda: {"kalshi": 100, "polymarket": 100})
    min_days_to_close: float = 2.0
    refresh_hours: float = Field(default=24.0, gt=0)
    polymarket_no_books: bool = True


class ArbCfg(_M):
    enabled: bool = True
    max_notional_per_trade: float = 100.0
    min_net_edge: float = 0.005


class MMCfg(_M):
    enabled: bool = True
    q_max: int = 100
    capital_max: float = 1000.0


class FLBCfg(_M):
    enabled: bool = True
    buckets: list[tuple[float, float]] = Field(default_factory=lambda: [(0.90, 0.97)])
    notional_per_market: float = 50.0


class StrategiesCfg(_M):
    arb_monitor: ArbCfg = ArbCfg()
    mm_quotes: MMCfg = MMCfg()
    flb_tilt: FLBCfg = FLBCfg()


class PeriodCfg(_M):
    start: str
    end: str
    hard_end: str


class PaperConfig(_M):
    """``config/paper.yaml``."""

    poll_interval_s: float = Field(default=30.0, gt=0)
    markets: MarketsCfg = MarketsCfg()
    strategies: StrategiesCfg = StrategiesCfg()
    min_days: int = 28
    period: PeriodCfg | None = None
    rate_limits_rps: dict[str, float] = Field(default_factory=dict)
    flush_every_cycles: int = Field(default=10, ge=1)
    disk_check_minutes: float = 10.0
    disk_warn_used_pct: float = 80.0


def load_paper_config(path: Path, settings: Settings | None = None) -> PaperConfig:
    """Load and validate the paper config."""
    return PaperConfig.model_validate(load_yaml(path, settings))
