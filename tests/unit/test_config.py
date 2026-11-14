"""Layer 1 unit tests: config env overrides, legacy fail-fast, and CORS origin derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from copixiv.config import SecurityConfig, _env_overrides, load_config


def test_env_overrides_nested_and_top_level_ignore_non_config(monkeypatch):
    monkeypatch.setenv("COPIXIV_PROXY__URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("COPIXIV_DATABASE_URL", "postgresql+psycopg2://u@localhost:5433/db")
    monkeypatch.setenv("COPIXIV_LOG_DIR", "/tmp/copixiv_logs")

    overrides = _env_overrides()
    assert overrides["proxy"] == {"url": "http://127.0.0.1:7890"}
    assert overrides["database_url"] == "postgresql+psycopg2://u@localhost:5433/db"
    assert "log_dir" not in overrides

    cfg = load_config("/nonexistent-path-for-test.yaml")
    assert cfg.proxy.url == "http://127.0.0.1:7890"
    assert cfg.database_url == "postgresql+psycopg2://u@localhost:5433/db"


@pytest.mark.parametrize(
    ("yaml_body", "expected_fragment"),
    [
        ("telegram:\n  token: abc\n", "telegram"),
        ("notifications:\n  - type: webhook\n    token: x\n", "webhook"),
        ("proxy:\n  http: http://127.0.0.1:7890\n", "proxy"),
    ],
)
def test_legacy_config_fails_fast_with_migration_hint(
    tmp_path: Path, monkeypatch, yaml_body: str, expected_fragment: str,
):
    for key in list(monkeypatch._setitem if hasattr(monkeypatch, "_setitem") else []):
        pass
    monkeypatch.delenv("COPIXIV_PROXY_HTTP", raising=False)
    monkeypatch.delenv("COPIXIV_PROXY_HTTPS", raising=False)
    cfg_file = tmp_path / "legacy.yaml"
    cfg_file.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file))
    assert expected_fragment in str(exc_info.value)


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
