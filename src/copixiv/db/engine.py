"""SQLAlchemy engine and session factory — created once at startup.

postgres-migration: the engine now targets PostgreSQL only (SQLite support
is removed).  The old SQLite PRAGMA kitchen sink (``journal_mode``,
``synchronous``, ``temp_store``, ``busy_timeout``, ``foreign_keys``,
``mmap_size``, ``cache_size``) is gone — those are SQLite-specific.  PG uses
WAL by default; the only per-connection tuning is a ``lock_timeout`` so a
stuck lock doesn't hang a request thread forever.  Database migrations are
still run via Alembic on init (replacing ``Base.metadata.create_all()``).
"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

DEFAULT_DATABASE_URL = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv"


def create_database_engine(
    database_url: str = DEFAULT_DATABASE_URL,
    echo: bool = False,
) -> Engine:
    """Create a SQLAlchemy engine for PostgreSQL with connection pooling.

    Args:
        database_url: SQLAlchemy URL (``postgresql+psycopg2://...``).
        echo: If True, log all SQL statements.

    Pool config: QueuePool(pool_size=6, max_overflow=12) — six persistent
    connections for concurrent reads plus up to 12 overflow connections for
    bursts.  Concurrency is kept in check by the page-handler semaphore in
    ``copixiv.features.novels.ingest``, so the pool only needs to cover that
    cap plus API traffic.  Each connection sets a ``lock_timeout`` via startup
    ``options`` so a blocked write fails fast instead of wedging the request,
    and so the setting persists across ``ROLLBACK``.
    """
    url = make_url(database_url)
    existing_options = url.query.get("options", "")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)
    if "lock_timeout" not in existing_options:
        merged_options = f"{existing_options} -c lock_timeout=5000".strip()
        url = url.update_query_dict({"options": merged_options})

    engine = create_engine(
        url,
        echo=echo,
        poolclass=QueuePool,
        pool_size=6,
        max_overflow=12,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a sessionmaker bound to *engine*.

    ``autoflush=False`` is deliberate: writes are executed only when the
    repository code says so, never implicitly on the next SELECT.  This
    keeps write timing predictable inside worker threads.

    Caveat: inside one transaction, a SELECT on a table you just wrote
    to does NOT see your own pending rows unless the write was flushed.
    When a code path needs "write then read the same table", call
    ``uow.session.flush()`` explicitly.
    """
    return sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=True,
        bind=engine,
    )


def run_migrations(
    database_url: str,
    project_root: str | None = None,
    *,
    engine: Engine | None = None,
) -> None:
    """Run Alembic migrations to bring the database schema up to date.

    Replaces the old ``Base.metadata.create_all()`` approach. Safe to call
    on an existing database — Alembic only applies unapplied migrations.

    Args:
        database_url: PostgreSQL SQLAlchemy URL.
        project_root: Root directory containing alembic.ini.  Defaults to
            searching upward from this file's location.
        engine: Optional pre-existing SQLAlchemy :class:`Engine` to reuse
            for the online migration connection instead of creating a new one.
    """
    from alembic import command
    from alembic.config import Config

    if project_root is None:
        p = Path(__file__).resolve().parent
        while not (p / "alembic.ini").exists() and p != p.parent:
            p = p.parent
        project_root = str(p)

    alembic_cfg = Config(str(Path(project_root) / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    if engine is not None:
        with engine.connect() as connection:
            alembic_cfg.attributes["connection"] = connection
            command.upgrade(alembic_cfg, "head")
    else:
        command.upgrade(alembic_cfg, "head")


def init_database(engine: Engine, database_url: str | None = None) -> None:
    """Run Alembic migrations using *engine* to bring the database schema up to date."""
    url = database_url or engine.url.render_as_string(hide_password=False)
    run_migrations(url, engine=engine)
