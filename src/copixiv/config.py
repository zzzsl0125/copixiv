"""Application configuration — Pydantic Settings with YAML + env support.

Load order: defaults → YAML file → environment variables (prefix ``COPIXIV_``).
The config is accessed via :func:`get_config`, which lazily loads and caches
the ``AppConfig`` singleton on first call.
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class PathConfig(BaseModel):
    """Filesystem paths for data and tokens.

    ``database`` is **DEPRECATED** (postgres-migration): the database is now a
    PostgreSQL server addressed by ``AppConfig.database_url``, not a file path.
    The field is retained only so an existing ``config.yaml`` that still lists
    ``path.database`` keeps parsing; it is ignored by the application.  Remove
    it in a later cleanup pass.
    """

    token: str = "pixiv_token.py"
    database: str = "database/database.db"  # DEPRECATED — see AppConfig.database_url
    download: str = "download"


class PixivClientConfig(BaseModel):
    """Rate-limiting and concurrency settings for the Pixiv client."""

    min_interval: int = 2  # seconds between requests
    cooling_duration: int = 120  # seconds after rate-limit hit
    max_concurrency: int = 5


class ProxyConfig(BaseModel):
    """Proxy URL applied to both HTTP and HTTPS traffic.

    A single ``url`` replaces the old ``http``/``https`` pair: pixivpy3's
    per-account API and the anonymous image session both accept one proxy
    for both schemes.
    """

    url: str = ""


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
    - ``allowed_origins``: CORS origin whitelist (no wildcard).  When it is
      not set explicitly it is derived from ``allowed_hosts`` — each host is
      allow-listed on the dev ports 5173/4173 — and with no hosts it falls
      back to the local dev origins.
    """

    api_key: str = ""  # 空=关闭 API key 校验
    allowed_hosts: list[str] = Field(default_factory=list)  # 额外放行的域名/IP
    allowed_origins: list[str] | None = None  # 空=由 allowed_hosts 推导

    @model_validator(mode="after")
    def _derive_allowed_origins(self) -> "SecurityConfig":
        if self.allowed_origins is not None:
            return self
        if self.allowed_hosts:
            origins: list[str] = []
            for host in self.allowed_hosts:
                origins.append(f"http://{host}:5173")
                origins.append(f"http://{host}:4173")
            self.allowed_origins = origins
        else:
            self.allowed_origins = [
                "http://localhost:5173", "http://127.0.0.1:5173",
                "http://localhost:4173", "http://127.0.0.1:4173",
            ]
        return self


class NotificationBackendConfig(BaseModel):
    """A single notification backend (one class serves both kinds).

    The unused fields stay empty: a ``telegram`` entry fills ``token`` and
    ``chat_id``; a ``webhook`` entry fills ``url``.  ``type`` is a
    ``Literal`` so an unknown backend fails fast at validation rather than
    silently doing nothing.
    """

    type: Literal["telegram", "webhook"]
    token: str = ""
    chat_id: str | int = ""
    url: str = ""


class AppConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    # PostgreSQL connection string (postgres-migration).  This replaces the
    # SQLite ``path.database`` as the single source of truth for how the app
    # reaches its database.  Default targets the local dev instance on port
    # 5433 (see scripts/pg_dev.py); a real deployment overrides it via
    # ``config.yaml`` ``database_url`` (or, once supported, an env override).
    database_url: str = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv"

    path: PathConfig = Field(default_factory=PathConfig)
    pixiv_client: PixivClientConfig = Field(default_factory=PixivClientConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    batch_download: BatchDownloadConfig = Field(
        default_factory=BatchDownloadConfig
    )
    backup: BackupConfig = Field(default_factory=BackupConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    notifications: list[NotificationBackendConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path("config.yaml")


def _env_overrides() -> dict:
    """Read COPIXIV_-prefixed env vars and nest them into the config shape.

    Example: ``COPIXIV_PROXY__URL=http://…`` → ``{"proxy": {"url": "…"}}``
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


# Old config keys → the migration hint shown when one is still present.
_LEGACY_REMAPPING = {
    "telegram": "合并为 notifications 列表（示例见 config.example.yaml）",
    "notifiers": "合并为 notifications 列表（示例见 config.example.yaml）",
    "webhook": "合并为 notifications 列表（示例见 config.example.yaml）",
    "frontend": "默认值已内置前端源码（无需配置）",
}


def _fail_fast_on_legacy_config(raw: dict) -> None:
    """Refuse to load configs still using the pre-K8 (config-cleanup) keys.

    The config format was consolidated in K8: notifications became a list,
    proxy collapsed to a single ``url``, and the frontend defaults moved
    into the client bundle.  Any residual old key now fails fast — with an
    actionable migration hint instead of a generic validation error —
    rather than being silently ignored.
    """
    reasons: list[str] = []

    for old_key, instruction in _LEGACY_REMAPPING.items():
        if old_key in raw:
            reasons.append(f"- {old_key!r} → {instruction}")

    proxy = raw.get("proxy")
    if isinstance(proxy, dict) and ("http" in proxy or "https" in proxy):
        reasons.append("- 旧 proxy 的 http/https 双键 → 单键 proxy.url（env: COPIXIV_PROXY_URL）")

    if os.environ.get("COPIXIV_PROXY_HTTP") or os.environ.get("COPIXIV_PROXY_HTTPS"):
        reasons.append("- COPIXIV_PROXY_HTTP/HTTPS → COPIXIV_PROXY_URL")

    if not reasons:
        return

    raise SystemExit(
        "配置已迁移（配置格式在 2026-08 收尾中变更）：\n"
        f"{chr(10).join(reasons)}\n"
        "请按新格式更新 config.yaml；示例见 config.example.yaml。"
    )


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from YAML file (if present), then apply env overrides.

    Raises:
        SystemExit: If the YAML file uses a legacy key, or is malformed.
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

    # Fail fast on the pre-K8 config keys BEFORE model validation, so the
    # migration hint (not a generic "unexpected field" error) is shown.
    _fail_fast_on_legacy_config(raw)

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
