from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pmr.settings import (
    PmrConfig,
    Settings,
    SettingsError,
    interpolate,
    load_config,
    load_yaml,
)
from tests.conftest import REPO


@pytest.fixture(autouse=True)
def _no_data_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest points PMR_DATA_DIR at tmp for every other test; these tests set it explicitly.
    monkeypatch.delenv("PMR_DATA_DIR", raising=False)


def S(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_defaults(settings: Settings) -> None:
    assert settings.owner_country == "KY"
    assert settings.metaculus_api_token is None
    assert settings.kalshi_api_base.startswith("https://")
    assert not settings.kalshi_auth_configured


def test_env_file_parsing_with_inline_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text((REPO / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.owner_country == "KY"
    assert s.metaculus_api_token is None
    assert s.data_root == "~/data/prediction-market-research"
    assert s.pmr_user_agent.startswith("prediction-market-research/0.1 (+https://github.com/")
    assert s.pmr_log_level == "INFO"


def test_secret_never_in_repr() -> None:
    s = S(metaculus_api_token="abc123secret")
    assert "abc123secret" not in repr(s)
    assert "abc123secret" not in str(s.model_dump())


def test_country_code_validated() -> None:
    assert S(owner_country=" ky ").owner_country == "KY"
    with pytest.raises(ValidationError):
        S(owner_country="Cayman")


def test_kalshi_auth_flag() -> None:
    assert S(kalshi_api_key_id="id", kalshi_private_key_path="/k.pem").kalshi_auth_configured


def test_data_dir_order(tmp_path: Path) -> None:
    assert S().data_dir(platform="linux", cwd=tmp_path) == tmp_path / "data"
    srv, xy = tmp_path / "srv", tmp_path / "x" / "y"
    assert S(data_root=str(srv)).data_dir(platform="linux") == srv
    assert S(data_root=str(srv), pmr_data_dir=str(xy)).data_dir(platform="linux") == xy
    assert S(pmr_data_dir="rel").data_dir(platform="linux", cwd=tmp_path) == tmp_path / "rel"


def test_tilde_expanded_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/ze")
    d = S(data_root="~/data/pmr").data_dir(platform="linux")
    assert d.as_posix().endswith("data/pmr") and "~" not in d.as_posix()


def test_windows_ignores_tilde_data_root_and_refuses_c(tmp_path: Path) -> None:
    s = S(data_root="~/data/pmr")
    assert s.data_dir(platform="win32", cwd=Path("D:/repo")) == Path("D:/repo") / "data"
    with pytest.raises(SettingsError, match="C:"):
        S().data_dir(platform="win32", cwd=Path("C:/Users/x"))
    with pytest.raises(SettingsError, match="C:"):
        S(pmr_warehouse_path="C:/w.duckdb").warehouse_path(platform="win32")


def test_warehouse_path(tmp_path: Path) -> None:
    assert (
        S().warehouse_path(platform="linux", cwd=tmp_path) == tmp_path / "data" / "warehouse.duckdb"
    )
    w = tmp_path / "w.duckdb"
    assert S(pmr_warehouse_path=str(w)).warehouse_path(platform="linux") == w


def test_interpolate(monkeypatch: pytest.MonkeyPatch) -> None:
    s = S(kalshi_api_base="https://k.example/v2")
    assert interpolate("${KALSHI_API_BASE}/x", s) == "https://k.example/v2/x"
    monkeypatch.setenv("SOME_VAR", "v")
    assert interpolate("a ${SOME_VAR} b", s) == "a v b"
    assert interpolate("${MISSING_VAR:-dflt}", s) == "dflt"
    with pytest.raises(SettingsError, match="not set"):
        interpolate("${MISSING_VAR}", s)
    with pytest.raises(SettingsError, match="secret"):
        interpolate("${METACULUS_API_TOKEN}", S(metaculus_api_token="t"))


def test_shipped_config_loads(cfg: PmrConfig) -> None:
    assert cfg.seed == 20261030
    assert cfg.venues.kalshi.base_url == "https://external-api.kalshi.com/trade-api/v2"
    assert cfg.matching.embed_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert cfg.matching.auto_confirm is False
    assert cfg.calibration.bootstrap.n == 2000
    assert cfg.http.max_attempts == 6


def test_every_yaml_in_config_parses(settings: Settings) -> None:
    for p in sorted((REPO / "config").glob("*.yaml")):
        assert isinstance(load_yaml(p, settings), dict), p


def test_bad_configs_rejected(tmp_path: Path, settings: Settings) -> None:
    text = (REPO / "config" / "pmr.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text.replace("auto_confirm: false", "auto_confirm: true"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad, settings)
    bad.write_text(text.replace("0.05, 0.10, 0.20", "0.10, 0.05, 0.20"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad, settings)
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="mapping"):
        load_yaml(bad, settings)
