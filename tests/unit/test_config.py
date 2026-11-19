"""Layer 1 unit tests: pydantic-settings config merge, legacy fail-fast, and offline OpenAPI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import copixiv.config as config_mod
from copixiv.config import AppConfig, PathConfig, SecurityConfig, load_config


def test_env_overrides_yaml_deep_merge_lists_and_ignores_unrelated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg_file = tmp_path / "app.yaml"
    cfg_file.write_text(
        "backup:\n  dir: from_yaml\n  keep_count: 10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COPIXIV_PROXY__URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("COPIXIV_BACKUP__DIR", "/tmp/from_env")
    monkeypatch.setenv("COPIXIV_SECURITY__ALLOWED_HOSTS", '["nas.local", "box.local"]')
    monkeypatch.setenv("COPIXIV_DATABASE_URL", "postgresql+psycopg2://u@localhost:5433/db")
    monkeypatch.setenv("COPIXIV_LOG_DIR", "/tmp/copixiv_logs")

    cfg = load_config(str(cfg_file))
    assert cfg.proxy.url == "http://127.0.0.1:7890"
    assert cfg.backup.dir == "/tmp/from_env"
    assert cfg.backup.keep_count == 10  # preserved from YAML
    assert cfg.security.allowed_hosts == ["nas.local", "box.local"]
    assert cfg.database_url == "postgresql+psycopg2://u@localhost:5433/db"

    # Direct AppConfig() does not implicitly read config.yaml from CWD
    direct = AppConfig()
    assert direct.backup.dir == "/tmp/from_env"
    assert direct.backup.keep_count == 4


@pytest.mark.parametrize(
    ("yaml_body", "expected_fragment"),
    [
        ("telegram:\n  token: abc\n", "telegram"),
        ("notifications:\n  - type: webhook\n    token: x\n", "webhook"),
        ("proxy:\n  http: http://127.0.0.1:7890\n", "proxy"),
    ],
)
def test_legacy_config_fails_fast_with_migration_hint(
    tmp_path: Path,
    monkeypatch,
    yaml_body: str,
    expected_fragment: str,
):
    monkeypatch.delenv("COPIXIV_PROXY_HTTP", raising=False)
    monkeypatch.delenv("COPIXIV_PROXY_HTTPS", raising=False)
    cfg_file = tmp_path / "legacy.yaml"
    cfg_file.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file))
    assert expected_fragment in str(exc_info.value)


def test_removed_query_engine_setting_fails_fast(tmp_path: Path, monkeypatch):
    with pytest.raises(ValidationError, match="query_engine"):
        AppConfig.model_validate({"query_engine": "pg"})

    cfg_file = tmp_path / "with_engine.yaml"
    cfg_file.write_text("query_engine: pg\n", encoding="utf-8")
    monkeypatch.delenv("COPIXIV_QUERY_ENGINE", raising=False)
    with pytest.raises(SystemExit, match="query_engine"):
        load_config(str(cfg_file))

    monkeypatch.setenv("COPIXIV_QUERY_ENGINE", "pg")
    with pytest.raises(ValueError, match="COPIXIV_QUERY_ENGINE"):
        load_config("/nonexistent-path-for-test.yaml")


def test_security_config_derives_allowed_origins():
    default_sec = SecurityConfig()
    assert "http://localhost:9000" in (default_sec.allowed_origins or [])
    assert "http://127.0.0.1:5173" in (default_sec.allowed_origins or [])

    hosted = SecurityConfig(allowed_hosts=["nas.local"])
    assert hosted.allowed_origins == [
        "http://nas.local:9000",
        "http://nas.local:5173",
        "http://nas.local:4173",
    ]

    explicit = SecurityConfig(
        allowed_hosts=["nas.local"],
        allowed_origins=["https://custom.example"],
    )
    assert explicit.allowed_origins == ["https://custom.example"]

    disabled_cors = SecurityConfig(allowed_hosts=["nas.local"], allowed_origins=[])
    assert disabled_cors.allowed_origins == []


def test_no_singleton_and_offline_export_openapi_in_broken_cwd(tmp_path: Path):
    with pytest.raises(ValidationError):
        PathConfig.model_validate({"download": "dl", "unknown_field": True})

    assert not hasattr(config_mod, "_config")
    assert not hasattr(config_mod, "get_config")

    # Broken config.yaml in cwd must NOT break importing lifespan or exporting OpenAPI
    (tmp_path / "config.yaml").write_text("telegram:\n  token: old\n", encoding="utf-8")
    res_lifespan = subprocess.run(
        [sys.executable, "-c", "import copixiv.lifespan, copixiv.app"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_lifespan.returncode == 0, res_lifespan.stderr

    res_openapi = subprocess.run(
        [sys.executable, "-m", "copixiv.export_openapi"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_openapi.returncode == 0, res_openapi.stderr
    schema = json.loads(res_openapi.stdout)
    assert "paths" in schema
    assert "/api/novels" in schema["paths"]
