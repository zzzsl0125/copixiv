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

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool


def create_database_engine(
    database_url: str = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv",
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
    cap plus API traffic.  Each connection sets a ``lock_timeout`` so a
    blocked write fails fast instead of wedging the request.
    """
    engine = create_engine(
        database_url,
        echo=echo,
        poolclass=QueuePool,
        pool_size=6,
        max_overflow=12,
    )

    # Register the DML probe that drives the data-version epoch
    # (``copixiv.db.data_version``): it hooks the engine's cursor events
    # and marks a transaction as "mutated" when INSERT/UPDATE/DELETE/COPY
    # run, so read-only commits never invalidate caches.  Importing the
    # module triggers the listener registration.
    import copixiv.db.data_version  # noqa: F401

    @event.listens_for(engine, "connect")
    def _set_pg_opts(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        # Fail a request quickly rather than blocking indefinitely if it
        # hits a lock it cannot acquire.  MVCC means ordinary writes don't
        # need the old SQLite single-writer serialization; this is just a
        # safety valve for the rare long-running lock.
        cursor.execute("SET lock_timeout = '60s'")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a sessionmaker bound to *engine*.

    ``autoflush=False`` is deliberate: writes are executed only when the
    repository code says so, never implicitly on the next SELECT.  This
    keeps write timing predictable inside worker threads.

    Caveat: inside one transaction, a SELECT on a table you just wrote
    to does NOT see your own pending rows unless the write was flushed.
    When a code path needs "write then read the same table", call
    ``await uow.flush()`` explicitly (see :class:`SqlUnitOfWork`).
    """
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def run_migrations(database_url: str, project_root: str | None = None) -> None:
    """Run Alembic migrations to bring the database schema up to date.

    Replaces the old ``Base.metadata.create_all()`` approach. Safe to call
    on an existing database — Alembic only applies unapplied migrations.

    Args:
        database_url: PostgreSQL SQLAlchemy URL.
        project_root: Root directory containing alembic.ini.  Defaults to
            searching upward from this file's location.
    """
    from alembic.config import Config
    from alembic import command

    if project_root is None:
        p = Path(__file__).resolve().parent
        while not (p / "alembic.ini").exists() and p != p.parent:
            p = p.parent
        project_root = str(p)

    alembic_cfg = Config(str(Path(project_root) / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")


def init_database(engine: Engine, database_url: str | None = None) -> None:
    """Run Alembic migrations to bring the database schema up to date.

    This replaces the old ``Base.metadata.create_all()`` call.  The *engine*
    parameter is retained for backwards compatibility — the migration runner
    uses its own engine internally via *database_url*.
    """
    url = database_url or str(engine.url)
    run_migrations(url)
