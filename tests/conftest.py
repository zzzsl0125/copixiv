"""Shared test fixtures — SQLite engines/factories used across test modules.

Consolidates the previously copy-pasted fixtures (with drifting variants:
StaticPool vs file-backed, FK PRAGMA present or not) into one place.

Fixtures:
- ``sqlite_engine`` / ``session_factory``: in-memory StaticPool engine
  (single shared connection — safe for repos running in worker threads).
- ``file_engine`` / ``file_session_factory``: file-backed engine with
  WAL + busy_timeout, mirroring production's pragma setup.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base


def _enable_foreign_keys(engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_fk_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.fixture
def sqlite_engine():
    """In-memory engine shared across threads via StaticPool."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    _enable_foreign_keys(engine)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(sqlite_engine):
    return create_session_factory(sqlite_engine)


@pytest.fixture
def file_engine(tmp_path):
    """File-backed engine with WAL + busy_timeout (like production)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        pool_size=16,
        max_overflow=0,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def file_session_factory(file_engine):
    return create_session_factory(file_engine)
