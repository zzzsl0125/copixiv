"""Application configuration — Pydantic Settings with YAML + env support.

Load order: defaults → YAML file → environment variables (prefix ``COPIXIV_``).
The config is created as a module-level singleton via ``_load_config()``,
exactly matching the old project's convention so imports stay unchanged.
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

    token: str = "pixiv_token"
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


class PixivAccountsConfig(BaseModel):
    """Designated Pixiv accounts for specific operations (follow, etc.)."""

    follow: str = ""


class AppConfig(BaseModel):
    """Root configuration object."""

    path: PathConfig = Field(default_factory=PathConfig)
    pixiv_client: PixivClientConfig = Field(default_factory=PixivClientConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    pixiv_accounts: PixivAccountsConfig = Field(
        default_factory=PixivAccountsConfig
    )
    notify_on_new_novel: bool = True


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


def _load_config(config_path: str | None = None) -> AppConfig:
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


# Module-level singleton — same convention as the old project so call sites
# that do ``from copixiv.app.config import config`` continue to work.
config: AppConfig = _load_config()
