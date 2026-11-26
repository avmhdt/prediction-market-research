"""Settings: secrets and paths from ``.env`` plus the validated YAML run config.

Two layers:

* :class:`Settings` reads environment variables (and ``.env``) with pydantic-settings.
  Secrets are :class:`pydantic.SecretStr`, so they never appear in ``repr`` or logs.
* :class:`PmrConfig` validates ``config/pmr.yaml``. ``${NAME}`` and ``${NAME:-default}``
  placeholders in the YAML are filled from :class:`Settings` first, then the process
  environment.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/pmr.yaml")
DEFAULT_KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_USER_AGENT = (
    "prediction-market-research/0.1 (+https://github.com/avmhdt/prediction-market-research)"
)

Venue = Literal["kalshi", "polymarket", "manifold", "metaculus"]
VENUES: tuple[Venue, ...] = ("kalshi", "polymarket", "manifold", "metaculus")


def _on_c_drive(path: Path) -> bool:
    return PureWindowsPath(str(path)).drive.upper() == "C:"


class SettingsError(RuntimeError):
    """Raised when settings or the YAML config are invalid or unsafe."""


class Settings(BaseSettings):
    """Environment settings (SPEC section 3, ``.env.example``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    owner_country: str = "KY"
    metaculus_api_token: SecretStr | None = None
    kalshi_api_base: str = DEFAULT_KALSHI_API_BASE
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    dune_api_key: SecretStr | None = None
    polymarket_subgraph_url: str | None = None
    thegraph_api_key: SecretStr | None = None
    data_root: str | None = None
    pmr_data_dir: str | None = None
    pmr_warehouse_path: str | None = None
    pmr_embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hf_home: str | None = None
    pmr_user_agent: str = DEFAULT_USER_AGENT
    pmr_log_level: Literal["DEBUG", "INFO", "WARNING"] = "INFO"

    @field_validator("owner_country")
    @classmethod
    def _country_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("OWNER_COUNTRY must be an ISO 3166-1 alpha-2 code")
        return v

    @property
    def kalshi_auth_configured(self) -> bool:
        """True when both Kalshi key settings are present (FR-02)."""
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    def data_dir(self, *, platform: str | None = None, cwd: Path | None = None) -> Path:
        """Root of ``raw/``, ``interim/``, ``processed/`` and ``paper/``.

        Order: ``PMR_DATA_DIR``, then ``DATA_ROOT``, then ``./data``. On native Windows a
        ``DATA_ROOT`` starting with ``~`` is ignored, because ``~`` would resolve to ``C:``
        (SPEC section 7d); any data directory on drive ``C:`` is refused.
        """
        plat = platform or sys.platform
        base = cwd or Path.cwd()
        chosen: str | None = self.pmr_data_dir
        if chosen is None and self.data_root is not None:
            if plat == "win32" and self.data_root.strip().startswith("~"):
                chosen = None
            else:
                chosen = self.data_root
        if chosen:
            path = Path(os.path.expanduser(chosen))
            if not path.is_absolute():
                path = base / path
        else:
            path = base / "data"
        if plat == "win32" and _on_c_drive(path):
            raise SettingsError(f"data directory {path} is on C:, which is not allowed")
        return path

    def warehouse_path(self, *, platform: str | None = None, cwd: Path | None = None) -> Path:
        """The single DuckDB warehouse file (``<data dir>/warehouse.duckdb`` by default)."""
        if self.pmr_warehouse_path:
            path = Path(os.path.expanduser(self.pmr_warehouse_path))
            if (platform or sys.platform) == "win32" and _on_c_drive(path):
                raise SettingsError(f"warehouse path {path} is on C:, which is not allowed")
            return path
        return self.data_dir(platform=platform, cwd=cwd) / "warehouse.duckdb"


# --------------------------------------------------------------------------------------
# YAML run config (SPEC section 10)
# --------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StudyWindow(_Strict):
    from_: date = Field(alias="from")
    to: date | Literal["latest"] = "latest"


class KalshiVenue(_Strict):
    base_url: str
    rate_limit_rps: float = Field(gt=0)
    settlement_lag_hours: float = Field(ge=0)
    storage_allowed: bool = False  # owner ruling on Kalshi's data terms (docs/venue_notes.md)


class PolymarketVenue(_Strict):
    gamma_url: str
    clob_url: str
    data_url: str
    rate_limit_rps: float = Field(gt=0)
    settlement_lag_hours: float = Field(ge=0)
    onchain_source: Literal["dune", "subgraph", "none"] = "dune"


class ManifoldVenue(_Strict):
    base_url: str
    rate_limit_rps: float = Field(gt=0)
    min_unique_traders: int = Field(ge=0)


class MetaculusVenue(_Strict):
    base_url: str
    rate_limit_rps: float = Field(gt=0)


class Venues(_Strict):
    kalshi: KalshiVenue
    polymarket: PolymarketVenue
    manifold: ManifoldVenue
    metaculus: MetaculusVenue


class HttpConfig(_Strict):
    timeout_s: float = Field(gt=0)
    max_attempts: int = Field(ge=1, le=10)
    backoff_base_s: float = Field(gt=0)
    backoff_max_s: float = Field(gt=0)


class LiquidityFilter(_Strict):
    min_trades: int = Field(ge=0)
    min_notional: float = Field(ge=0)


class BootstrapConfig(_Strict):
    n: int = Field(ge=1)
    alpha: float = Field(gt=0, lt=1)
    cluster: str


class CalibrationConfig(_Strict):
    horizons: list[str]
    life_fraction: float = Field(gt=0, lt=1)
    max_spread: float = Field(gt=0, le=1)
    bins_equal_width: int = Field(ge=2)
    bins_equal_count: int = Field(ge=2)
    liquidity_filter: LiquidityFilter
    bootstrap: BootstrapConfig


class FlbConfig(_Strict):
    price_buckets: list[float]
    multiple_testing: Literal["holm"]

    @field_validator("price_buckets")
    @classmethod
    def _increasing(cls, v: list[float]) -> list[float]:
        if any(b <= a for a, b in zip(v, v[1:], strict=False)) or v[0] < 0 or v[-1] > 1:
            raise ValueError("price_buckets must be strictly increasing within [0, 1]")
        return v


class MatchingConfig(_Strict):
    close_window_days: int = Field(ge=0)
    embed_model: str
    embed_revision: str
    top_k: int = Field(ge=1)
    auto_confirm: Literal[False] = False


class ArbConfigYaml(_Strict):
    risk_free: Literal["fred_dtb3"]
    collateral_yield: dict[str, float]
    transfer_cost_per_contract: float = Field(ge=0)
    min_net_edge: float
    divergence_prior: tuple[float, float]


class MMConfig(_Strict):
    fair_value: Literal["ema_mid"]
    ema_halflife_s: float = Field(gt=0)
    half_spread: float = Field(gt=0)
    skew_gamma: float = Field(ge=0)
    q_max: int = Field(ge=1)
    capital_max: float = Field(gt=0)
    markouts_s: list[int]


class PmrConfig(_Strict):
    """Validated ``config/pmr.yaml``."""

    seed: int
    study_window: StudyWindow
    venues: Venues
    http: HttpConfig
    calibration: CalibrationConfig
    flb: FlbConfig
    matching: MatchingConfig
    arb: ArbConfigYaml
    mm: MMConfig


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate(text: str, settings: Settings) -> str:
    """Replace ``${NAME}`` / ``${NAME:-default}`` from settings, then ``os.environ``.

    Secret settings are never interpolated into config text.
    """

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        field = name.lower()
        if field in Settings.model_fields:
            value = getattr(settings, field)
            if isinstance(value, SecretStr):
                raise SettingsError(f"secret {name} cannot be used in config files")
            if value is not None:
                return str(value)
        env = os.environ.get(name)
        if env:
            return env
        if default is not None:
            return default
        raise SettingsError(f"config placeholder ${{{name}}} is not set")

    return _PLACEHOLDER.sub(repl, text)


def _fill(obj: Any, settings: Settings) -> Any:
    if isinstance(obj, str):
        return interpolate(obj, settings)
    if isinstance(obj, dict):
        return {k: _fill(v, settings) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, settings) for v in obj]
    return obj


def load_yaml(path: Path, settings: Settings | None = None) -> dict[str, Any]:
    """Read a YAML file, fill placeholders in string values and return the mapping."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must contain a YAML mapping")
    filled: dict[str, Any] = _fill(data, settings or Settings())
    return filled


def load_config(path: Path = DEFAULT_CONFIG_PATH, settings: Settings | None = None) -> PmrConfig:
    """Load and validate the main run config."""
    return PmrConfig.model_validate(load_yaml(path, settings))
