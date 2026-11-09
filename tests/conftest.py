from __future__ import annotations

from pathlib import Path

import pytest

from pmr.settings import PmrConfig, Settings, load_config

REPO = Path(__file__).resolve().parents[1]
ENV_VARS = (
    "OWNER_COUNTRY",
    "METACULUS_API_TOKEN",
    "KALSHI_API_BASE",
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "DUNE_API_KEY",
    "POLYMARKET_SUBGRAPH_URL",
    "THEGRAPH_API_KEY",
    "DATA_ROOT",
    "PMR_DATA_DIR",
    "PMR_WAREHOUSE_PATH",
    "PMR_EMBED_MODEL",
    "HF_HOME",
    "PMR_USER_AGENT",
    "PMR_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's .env or shell variables."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def cfg(settings: Settings) -> PmrConfig:
    return load_config(REPO / "config" / "pmr.yaml", settings)
