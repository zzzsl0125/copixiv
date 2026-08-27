"""Application configuration — Pydantic Settings with YAML + env support.

Load order: defaults → YAML file → environment variables (prefix ``COPIXIV_``).
The config is accessed via :func:`get_config`, which lazily loads and caches
the ``AppConfig`` singleton on first call.
"""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class PathConfig(BaseModel):
    """Filesystem paths for data and tokens."""

    token: str = "pixiv_token.py"
    database: str = "database/database.db"
    download: str = "download"


class PixivClientConfig(BaseModel):
    """Rate-limiting and concurrency settings for the Pixiv client."""

    min_interval: int = 2  # seconds between requests
    cooling_duration: int = 120  # seconds after rate-limit hit
    max_concurrency: int = 5


class TelegramConfig(BaseModel):
    """Telegram bot credentials for notifications."""

    token: str = ""
    chat_id: str | int = ""


class ProxyConfig(BaseModel):
    """HTTP/HTTPS proxy URLs."""

    http: str = ""
    https: str = ""


class FrontendConfig(BaseModel):
    """Default UI filter values."""

    default_min_like: int = 500
    default_min_text: int = 3000


class BatchDownloadConfig(BaseModel):
    """Naming template for batch-download ZIP internal paths.

    Available tokens:
        ``{id}`` (required), ``{title}``, ``{author_name}``, ``{author_id}``,
        ``{like}``, ``{view}``, ``{text}``, ``{date}``, ``{series_name}``,
        ``{series_index}``.
    """

    naming: str = "{author_name}/{series_name}/#{series_index}_{title}_{id}"


class BackupConfig(BaseModel):
    """Weekly database backup retention.

    ``keep_count`` weekly backups are retained (default 4).  Keeping a
    single copy gives no protection against accidental corruption being
    vacuumed into the only backup; N copies preserve a recovery window.
    """

    keep_count: int = 4


class SecurityConfig(BaseModel):
    """Security hardening settings for the web API.

    The server still listens on ``0.0.0.0`` (LAN access), so the trust
    boundary is pushed into these three layers instead:
    - ``api_key``: optional shared secret required on every ``/api/`` call
      (empty string disables it).
    - ``allowed_hosts``: extra Host-header values allowed past the
      Host-validation middleware (``localhost`` and IP literals always pass).
    - ``allowed_origins``: CORS origin whitelist (no wildcard).
    """

    api_key: str = ""  # 空=关闭 API key 校验
    allowed_hosts: list[str] = Field(default_factory=list)  # 额外放行的域名/IP
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ])


class NotifiersConfig(BaseModel):
    """Enabled notifier backends, in order (docs/MODULARITY.md §M6).

    Recognized names: ``telegram`` / ``webhook`` (mapped in
    ``notify/factory.py``).  Defaults to ``["telegram"]``
    — empty list disables notifications entirely.
    """

    enabled: list[str] = Field(default_factory=lambda: ["telegram"])


class WebhookConfig(BaseModel):
    """Webhook notifier backend settings (§M6 第二后端)."""

    url: str = ""  # 空=该后端跳过发送


class AppConfig(BaseModel):
    """Root configuration object."""

    path: PathConfig = Field(default_factory=PathConfig)
    pixiv_client: PixivClientConfig = Field(default_factory=PixivClientConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    batch_download: BatchDownloadConfig = Field(
        default_factory=BatchDownloadConfig
    )
    backup: BackupConfig = Field(default_factory=BackupConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    notifiers: NotifiersConfig = Field(default_factory=NotifiersConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path("config.yaml")


def _env_overrides() -> dict:
    """Read COPIXIV_-prefixed env vars and nest them into the config shape.

    Example: ``COPIXIV_PROXY_HTTP=http://…`` → ``{"proxy": {"http": "…"}}``
    """
    prefix = "COPIXIV_"
    overrides: dict = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("__", 1)
        if len(parts) == 2:
            section, field = parts
            overrides.setdefault(section, {})[field] = value
    return overrides


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into *base*."""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from YAML file (if present), then apply env overrides.

    Raises:
        SystemExit: If the YAML file exists but is malformed.
    """
    path = Path(config_path or _DEFAULT_CONFIG_PATH)

    raw: dict = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise SystemExit(f"Failed to parse {path}: {e}") from e

    raw = _deep_merge(raw, _env_overrides())

    try:
        return AppConfig(**raw)
    except Exception as e:
        raise SystemExit(
            f"Failed to construct AppConfig: {e}\n"
            f"Please check that all required fields are present."
        ) from e


# Lazy singleton — loaded on first access instead of at import time.
# This allows tests to import the module without a config.yaml file.
_config: AppConfig | None = None


def get_config(config_path: str | None = None) -> AppConfig:
    """Return the cached ``AppConfig`` singleton, loading it lazily.

    The first call triggers the YAML read; subsequent calls return the
    cached instance.  Pass *config_path* to load a non-default file.
    """
    global _config
    if _config is None:
        _config = load_config(config_path)
    return _config


def __getattr__(name: str):
    """Lazy module-level ``config`` alias for backwards compatibility.

    Allows ``from copixiv.config import config`` to work without
    triggering the YAML load at import time (which would raise
    ``SystemExit`` if ``config.yaml`` is missing, breaking test imports).
    The load is deferred until the first actual access of ``config``.
    """
    if name == "config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
