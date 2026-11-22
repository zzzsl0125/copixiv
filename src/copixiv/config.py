"""Application configuration — Pydantic Settings with YAML + env support.

Load order (highest precedence first):
  1. Explicit init kwargs (``AppConfig(...)``)
  2. Environment variables (prefix ``COPIXIV_``, nested delimiter ``__``)
  3. YAML file (loaded explicitly by :func:`load_config`, never implicitly
     on direct ``AppConfig()`` instantiation)
  4. Model defaults
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
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


def normalize_proxy_url(proxy_url: str | None) -> str | None:
    """Normalise a proxy string (``127.0.0.1:20172`` → ``http://127.0.0.1:20172``)."""
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        return f"http://{cleaned}"
    return cleaned


class ProxyConfig(BaseModel):
    """Proxy URL applied to both HTTP and HTTPS traffic."""

    model_config = ConfigDict(extra="forbid")

    url: str = ""

    def normalized_url(self) -> str | None:
        """Return the normalised proxy URL, or ``None`` when unconfigured."""
        return normalize_proxy_url(self.url)


class BatchDownloadConfig(BaseModel):
    """Naming template for batch-download ZIP internal paths."""

    model_config = ConfigDict(extra="forbid")

    naming: str = DEFAULT_NAMING_TEMPLATE


class BackupConfig(BaseModel):
    """Weekly database backup location."""

    model_config = ConfigDict(extra="forbid")

    dir: str = "backups"


class SecurityConfig(BaseModel):
    """Security hardening settings for the web API."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = ""  # 空=关闭 API key 校验
    allowed_hosts: list[str] = Field(default_factory=list)  # 额外放行的域名/IP
    allowed_origins: list[str] | None = None  # None=由 allowed_hosts 推导；[]=禁用跨域

    @model_validator(mode="after")
    def _derive_allowed_origins(self) -> SecurityConfig:
        if self.allowed_origins is None:
            hosts = self.allowed_hosts or ["localhost", "127.0.0.1"]
            self.allowed_origins = [f"http://{host}:{port}" for host in hosts for port in (9000, 5173, 4173)]
        return self


class NotificationBackendConfig(BaseModel):
    """A single notification backend (currently Telegram only)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["telegram"]
    token: str = ""
    chat_id: str | int = ""


_DEFAULT_CONFIG_PATH = Path("config.yaml")

# Scoped YAML path bound only inside load_config() so direct AppConfig(...)
# instantiation never touches CWD config.yaml.
_yaml_path_ctx: ContextVar[Path | None] = ContextVar("_yaml_path_ctx", default=None)


class _CheckedYamlSettingsSource(YamlConfigSettingsSource):
    """YAML source that converts parse errors to SystemExit."""

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
