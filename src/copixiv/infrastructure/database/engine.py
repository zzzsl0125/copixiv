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

    Pool config: QueuePool(pool_size=3, max_overflow=5) — three persistent
    connections for concurrent reads (WAL mode supports multiple readers)
    plus up to 5 overflow connections for bursts.
    """
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        echo=echo,
        poolclass=QueuePool,
        pool_size=3,
        max_overflow=5,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a sessionmaker bound to *engine*."""
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
