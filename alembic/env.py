"""Alembic environment configuration — reads database path from copixiv config."""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool, event
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
      2. ``alembic.ini`` ``sqlalchemy.url`` (if it's not the placeholder)
      3. copixiv ``config.yaml`` ``path.database`` (resolved relative to project root)
      4. ``alembic.ini`` ``sqlalchemy.url`` (as-is fallback)
    """
    # 1. Command-line -x url=... override
    url_override = context.get_x_argument(as_dictionary=True).get("url")
    if url_override:
        return url_override

    # 2. alembic.ini sqlalchemy.url (use if not the default placeholder)
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url and "driver://" not in ini_url:
        return ini_url

    # 3. copixiv config.yaml
    try:
        from copixiv.config import config as app_config
        db_path = app_config.path.database
        if not Path(db_path).is_absolute():
            db_path = str(_project_root / db_path)
        return f"sqlite:///{db_path}"
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
        render_as_batch=True,  # batch mode for SQLite ALTER support
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

    # Enable WAL + FK pragmas on every connection, including migrations
    @event.listens_for(connectable, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Keep temp B-trees (e.g. CREATE INDEX on 232k rows) in memory.
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
