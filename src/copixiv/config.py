"""Application configuration — Pydantic Settings with YAML + env support.

Load order (highest precedence first):
  1. Explicit init kwargs (``AppConfig(...)``)
  2. Environment variables (prefix ``COPIXIV_``, nested delimiter ``__``)
  3. YAML file (loaded explicitly by :func:`load_config`, never implicitly
     on direct ``AppConfig()`` instantiation)
  4. Model defaults
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from copixiv.core.naming import DEFAULT_NAMING_TEMPLATE

# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class PathConfig(BaseModel):
    """Filesystem paths for stored novel data."""

    model_config = ConfigDict(extra="forbid")

    download: str = "download"


class PixivClientConfig(BaseModel):
    """Rate-limiting and concurrency settings for the Pixiv client."""

    model_config = ConfigDict(extra="forbid")

    min_interval: int = 2  # seconds between requests
    cooling_duration: int = 120  # seconds after rate-limit hit
    max_concurrency: int = 5


class ProxyConfig(BaseModel):
    """Proxy URL applied to both HTTP and HTTPS traffic."""

    model_config = ConfigDict(extra="forbid")

    url: str = ""


class BatchDownloadConfig(BaseModel):
    """Naming template for batch-download ZIP internal paths."""

    model_config = ConfigDict(extra="forbid")

    naming: str = DEFAULT_NAMING_TEMPLATE


class BackupConfig(BaseModel):
    """Weekly database backup location and retention."""

    model_config = ConfigDict(extra="forbid")

    dir: str = "backups"
    keep_count: int = 4


class SecurityConfig(BaseModel):
    """Security hardening settings for the web API."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = ""  # 空=关闭 API key 校验
    allowed_hosts: list[str] = Field(default_factory=list)  # 额外放行的域名/IP
    allowed_origins: list[str] | None = None  # None=由 allowed_hosts 推导；[]=禁用跨域

    @model_validator(mode="after")
    def _derive_allowed_origins(self) -> SecurityConfig:
        if self.allowed_origins is not None:
            return self
        if self.allowed_hosts:
            origins: list[str] = []
            for host in self.allowed_hosts:
                origins.append(f"http://{host}:9000")
                origins.append(f"http://{host}:5173")
                origins.append(f"http://{host}:4173")
            self.allowed_origins = origins
        else:
            self.allowed_origins = [
                "http://localhost:9000",
                "http://127.0.0.1:9000",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:4173",
                "http://127.0.0.1:4173",
            ]
        return self


class NotificationBackendConfig(BaseModel):
    """A single notification backend (currently Telegram only)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["telegram"]
    token: str = ""
    chat_id: str | int = ""

    @field_validator("type", mode="before")
    @classmethod
    def _reject_removed_webhook(cls, value: object) -> object:
        if value == "webhook":
            raise ValueError(
                "webhook 通知后端已移除（本版本只支持 telegram）；"
                "请从 config.yaml 的 notifications 列表中删除该条目"
            )
        return value


_DEFAULT_CONFIG_PATH = Path("config.yaml")

# Scoped YAML path bound only inside load_config() so direct AppConfig(...)
# instantiation never touches CWD config.yaml.
_yaml_path_ctx: ContextVar[Path | None] = ContextVar("_yaml_path_ctx", default=None)

_LEGACY_REMAPPING = {
    "telegram": "合并为 notifications 列表（示例见 config.example.yaml）",
    "notifiers": "合并为 notifications 列表（示例见 config.example.yaml）",
    "webhook": "合并为 notifications 列表（示例见 config.example.yaml）",
    "frontend": "默认值已内置前端源码（无需配置）",
}


def _check_legacy_env() -> None:
    """Reject removed environment variables early with clear hints."""
    if "COPIXIV_QUERY_ENGINE" in os.environ:
        raise ValueError("环境变量 COPIXIV_QUERY_ENGINE 已移除，系统固定使用 Rust 单检索引擎，请取消设置")
    if os.environ.get("COPIXIV_PROXY_HTTP") or os.environ.get("COPIXIV_PROXY_HTTPS"):
        raise SystemExit(
            "配置已迁移（配置格式在 2026-08 收尾中变更）：\n"
            "- COPIXIV_PROXY_HTTP/HTTPS → COPIXIV_PROXY__URL\n"
            "请按新格式更新环境变量或 config.yaml；示例见 config.example.yaml。"
        )


def _fail_fast_on_legacy_yaml(raw: dict[str, Any]) -> None:
    """Refuse to load YAML configs still using pre-K8 keys."""
    reasons: list[str] = []

    for old_key, instruction in _LEGACY_REMAPPING.items():
        if old_key in raw:
            reasons.append(f"- {old_key!r} → {instruction}")

    proxy = raw.get("proxy")
    if isinstance(proxy, dict) and ("http" in proxy or "https" in proxy):
        reasons.append("- 旧 proxy 的 http/https 双键 → 单键 proxy.url（env: COPIXIV_PROXY__URL）")

    if not reasons:
        return

    raise SystemExit(
        "配置已迁移（配置格式在 2026-08 收尾中变更）：\n"
        f"{chr(10).join(reasons)}\n"
        "请按新格式更新 config.yaml；示例见 config.example.yaml。"
    )


class _CheckedYamlSettingsSource(YamlConfigSettingsSource):
    """YAML source that converts parse errors and legacy keys to SystemExit."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        yaml_file: Path,
        yaml_file_encoding: str = "utf-8",
    ) -> None:
        try:
            super().__init__(
                settings_cls,
                yaml_file=yaml_file,
                yaml_file_encoding=yaml_file_encoding,
            )
        except yaml.YAMLError as exc:
            raise SystemExit(f"Failed to parse {yaml_file}: {exc}") from exc

        if not isinstance(self.yaml_data, dict):
            raise SystemExit(f"Failed to parse {yaml_file}: expected mapping at root")
        _fail_fast_on_legacy_yaml(self.yaml_data)


class AppConfig(BaseSettings):
    """Root configuration object backed by ``pydantic-settings``."""

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="COPIXIV_",
        env_nested_delimiter="__",
    )

    database_url: str = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv"

    path: PathConfig = Field(default_factory=PathConfig)
    pixiv_client: PixivClientConfig = Field(default_factory=PixivClientConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    batch_download: BatchDownloadConfig = Field(default_factory=BatchDownloadConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    notifications: list[NotificationBackendConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_query_engine(cls, data: Any) -> Any:
        if isinstance(data, dict) and "query_engine" in data:
            raise ValueError(
                "配置项 'query_engine' 已移除，系统固定使用 Rust 单检索引擎，请从 config.yaml 删除该键"
            )
        return data

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings, file_secret_settings
        yaml_path = _yaml_path_ctx.get()
        if yaml_path is not None and yaml_path.exists():
            return (
                init_settings,
                env_settings,
                _CheckedYamlSettingsSource(
                    settings_cls,
                    yaml_file=yaml_path,
                    yaml_file_encoding="utf-8",
                ),
            )
        return (init_settings, env_settings)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML file (if present) merged with ``COPIXIV_`` env vars."""
    _check_legacy_env()
    path = Path(config_path or _DEFAULT_CONFIG_PATH)
    token = _yaml_path_ctx.set(path)
    try:
        return AppConfig()
    except ValidationError as exc:
        raise SystemExit(
            f"Failed to construct AppConfig: {exc}\nPlease check that all required fields are present."
        ) from exc
    finally:
        _yaml_path_ctx.reset(token)
