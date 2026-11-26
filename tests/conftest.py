from __future__ import annotations

import socket
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
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never see the developer's .env or shell variables, and write data only to tmp."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PMR_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _reset_logging() -> object:
    """A CLI test configures structlog to print to its captured stream; reset it after every
    test so later tests never log to a closed file."""
    import structlog

    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _quiet_build_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The warehouse guard sees plenty of memory, no disk errors and no write counter, so a
    test never depends on the machine it runs on (tests that check a reason patch these)."""
    from pmr.warehouse import guard

    monkeypatch.setattr(guard, "mem_available_gb", lambda: 1000.0)
    monkeypatch.setattr(guard, "ext4_errors", dict)
    monkeypatch.setattr(guard, "written_bytes", lambda _path=None: None)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real outbound connection fails unless the test is marked ``network``."""
    if request.node.get_closest_marker("network"):
        return
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if str(host) not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise RuntimeError(f"test tried to open a network connection to {host!r}")
        real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def cfg(settings: Settings) -> PmrConfig:
    return load_config(REPO / "config" / "pmr.yaml", settings)
