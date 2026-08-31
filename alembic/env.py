"""Alembic environment configuration — reads the database URL from copixiv config."""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Alembic Config object
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Target metadata — all ORM models registered on Base
# ---------------------------------------------------------------------------
from copixiv.db.models import Base
target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Resolve database URL from copixiv config or fall back to alembic.ini
# ---------------------------------------------------------------------------
# Project root is the parent of the alembic/ directory
_project_root = Path(__file__).resolve().parent.parent
# Add src/ to sys.path so copixiv is importable
sys.path.insert(0, str(_project_root / "src"))


def _get_database_url() -> str:
    """Resolve the sqlalchemy URL.

    Priority:
      1. Command-line ``-x url=...`` override
      2. ``alembic.ini`` ``sqlalchemy.url`` (if it's a real PG URL — the
         ``engine.run_migrations`` path sets this) [replaces the old SQLite
         ``path.database`` resolution]
      3. copixiv ``config.yaml`` ``database_url``
      4. ``alembic.ini`` ``sqlalchemy.url`` (as-is fallback)
    """
    # 1. Command-line -x url=... override
    url_override = context.get_x_argument(as_dictionary=True).get("url")
    if url_override:
        return url_override

    # 2. alembic.ini sqlalchemy.url (PG URL set by engine.run_migrations).
    #    The default alembic.ini still carries the old SQLite placeholder
    #    (``sqlite:///...``); ignore that and resolve from app config.
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url and not ini_url.startswith("sqlite:///") and "://" in ini_url:
        return ini_url

    # 3. copixiv config.yaml database_url
    try:
        from copixiv.config import config as app_config
        return app_config.database_url
    except Exception:
        pass

    # 4. Fallback
    return ini_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits SQL to the script output without connecting to a database.
    """
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database."""
    url = _get_database_url()

    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
