"""Alembic environment configuration — reads the database URL from copixiv config."""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Alembic Config object
config = context.config

# Set up Python logging from alembic.ini without silencing existing loggers (e.g. loguru).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# ---------------------------------------------------------------------------
# Ensure src/ is importable BEFORE importing copixiv models
# ---------------------------------------------------------------------------
_project_root = Path(__file__).resolve().parent.parent
_src_dir = str(_project_root / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# ---------------------------------------------------------------------------
# Target metadata — all ORM models registered on Base
# ---------------------------------------------------------------------------
from copixiv.db.engine import DEFAULT_DATABASE_URL  # noqa: E402
from copixiv.db.models import Base  # noqa: E402

target_metadata = Base.metadata


def _get_database_url() -> str:
    """Resolve the sqlalchemy URL.

    Priority:
      1. Command-line ``-x url=...`` override
      2. ``alembic.ini`` ``sqlalchemy.url`` (if non-empty PG URL — the
         ``engine.run_migrations`` path sets this)
      3. copixiv ``config.yaml`` / ``COPIXIV_DATABASE_URL`` via ``load_config()``
      4. Default local PG URL fallback
    """
    # 1. Command-line -x url=... override
    url_override = context.get_x_argument(as_dictionary=True).get("url")
    if url_override:
        return url_override

    # 2. alembic.ini sqlalchemy.url (set at runtime by engine.run_migrations).
    ini_url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if ini_url and "://" in ini_url:
        return ini_url

    # 3. copixiv config.yaml / env override
    try:
        from copixiv.config import load_config

        return load_config().database_url
    except Exception:
        pass

    # 4. Fallback
    return ini_url or DEFAULT_DATABASE_URL


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
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    url = _get_database_url()

    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
