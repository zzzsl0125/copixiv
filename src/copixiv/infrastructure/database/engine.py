"""SQLAlchemy engine and session factory — created once at startup.

Database migrations are run via Alembic on init, replacing the old
``Base.metadata.create_all()`` approach.
"""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool


def create_database_engine(
    database_path: str = "database/database.db",
    echo: bool = False,
) -> Engine:
    """Create a SQLAlchemy engine for SQLite with WAL mode and connection pooling.

    Args:
        database_path: Path to the SQLite database file.
        echo: If True, log all SQL statements.

    Pool config: QueuePool(pool_size=6, max_overflow=12) — six persistent
    connections for concurrent reads (WAL mode supports multiple readers)
    plus up to 12 overflow connections for bursts.  Concurrency is kept
    in check by the page-handler semaphore in ``tasks/pipeline.py``, so
    the pool only needs to cover that cap plus API traffic.
    """
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        echo=echo,
        poolclass=QueuePool,
        pool_size=6,
        max_overflow=12,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Writes are serialized in-process via db_write() (see
        # infrastructure/database/write_lock.py), so this timeout is only
        # a fallback for short external writers (e.g. FastAPI sessions).
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA foreign_keys=ON")
        # mmap the main DB file into the process address space so page
        # access is a pointer dereference instead of a pread() syscall.
        # 512 MB covers the current ~508 MB DB; growth beyond this
        # silently falls back to read(). WAL is not mmap'd either way.
        cursor.execute("PRAGMA mmap_size=536870912")
        # Per-connection pager cache (~50 MB). Unlike the OS page cache,
        # this lives on the process heap and is never reclaimed by Linux,
        # so the pool_size persistent connections stay warm across idle
        # periods. 50 MB is enough to hold the hot index B-tree nodes.
        cursor.execute("PRAGMA cache_size=-50000")
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


def run_migrations(database_path: str, project_root: str | None = None) -> None:
    """Run Alembic migrations to bring the database schema up to date.

    Replaces the old ``Base.metadata.create_all()`` approach. Safe to call
    on an existing database — Alembic only applies unapplied migrations.

    Args:
        database_path: Path to the SQLite database file (absolute recommended).
        project_root: Root directory containing alembic.ini.  Defaults to
            searching upward from this file's location.
    """
    from alembic.config import Config
    from alembic import command

    # Resolve database path to absolute so env.py can use it directly
    db_path_abs = str(Path(database_path).resolve())

    if project_root is None:
        p = Path(__file__).resolve().parent
        while not (p / "alembic.ini").exists() and p != p.parent:
            p = p.parent
        project_root = str(p)

    alembic_cfg = Config(str(Path(project_root) / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path_abs}")

    command.upgrade(alembic_cfg, "head")


def init_database(engine: Engine, database_path: str | None = None) -> None:
    """Run Alembic migrations to bring the database schema up to date.

    This replaces the old ``Base.metadata.create_all()`` call.  The *engine*
    parameter is retained for backwards compatibility — the migration runner
    uses its own engine internally.
    """
    db_path = database_path or str(engine.url).replace("sqlite:///", "")
    run_migrations(db_path)
