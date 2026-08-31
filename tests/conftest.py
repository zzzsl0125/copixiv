"""Shared test fixtures — PostgreSQL-backed (postgres-migration).

Consolidates the previously SQLite-specific fixtures into a single
PostgreSQL-driven set.  The ``sqlite_engine`` / ``file_engine`` / … fixtures
are gone (their referencing tests are rewritten in phase 2).

A session-scoped autouse fixture guarantees the local ``pgserver`` instance is
running, the ``copixiv_test`` database exists, and it is Alembic-migrated to
head.  ``session_factory`` keeps its name (now backed by a PG engine); a
function-scoped ``clean_db`` fixture truncates all application tables so tests
start from a fresh state.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from copixiv.db.engine import create_session_factory, run_migrations

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PGDEV_SCRIPT = PROJECT_ROOT / "scripts" / "pg_dev.py"

TEST_DB = "copixiv_test"
TEST_DATABASE_URL = f"postgresql+psycopg2://postgres@127.0.0.1:5433/{TEST_DB}"

# All application tables, in any order (TRUNCATE ... CASCADE handles FKs).
_ALL_TABLES = (
    "novel", "author", "series", "tag", "tag_alias", "tag_preference",
    "failed_novel", "novel_search", "scheduled_task", "task_history",
    "token", "setting", "search_history",
)


def _pgdev(*args: str):
    subprocess.run([sys.executable, str(PGDEV_SCRIPT), *args], check=True)


def _start_pg() -> None:
    """Ensure the local Postgres instance is running (starts if needed)."""
    _pgdev("start")


def _ensure_db(name: str) -> None:
    """Create the database if it does not already exist."""
    _pgdev("createdb", name)


def _migrate(url: str) -> None:
    """Apply Alembic head to the database (idempotent)."""
    run_migrations(url)


@pytest.fixture(scope="session", autouse=True)
def _pg_session_setup():
    """Bring up the dev PostgreSQL instance and a migrated test database."""
    _start_pg()
    _ensure_db(TEST_DB)
    _migrate(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def pg_engine():
    """A session-scoped engine bound to the migrated ``copixiv_test`` DB."""
    engine = create_engine(
        TEST_DATABASE_URL, pool_size=5, max_overflow=10, echo=False
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def pg_session_factory(pg_engine):
    """Session factory bound to the session-scoped PG engine."""
    return create_session_factory(pg_engine)


@pytest.fixture
def session_factory(pg_session_factory):
    """``session_factory`` — kept name, now PG-backed (fresh per test)."""
    return pg_session_factory


@pytest.fixture
def clean_db(pg_engine):
    """Truncate all application tables, restarting identity sequences."""
    table_list = ", ".join(_ALL_TABLES)
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
    yield
