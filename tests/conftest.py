"""Shared test fixtures — PostgreSQL-backed (postgres-migration).

Consolidates the previously SQLite-specific fixtures into a single
PostgreSQL-driven set.  The ``sqlite_engine`` / ``file_engine`` / … fixtures
are gone (their referencing tests are rewritten in phase 2).

A session-scoped autouse fixture guarantees the local ``pgserver`` instance is
running, the ``copixiv_test`` database exists, and it is Alembic-migrated to
head.  ``session_factory`` keeps its name (now backed by a PG engine); a
function-scoped ``clean_db`` fixture truncates all application tables so tests
start from a fresh state.

``seeded_db`` seeds a 200-novel sample: from the legacy SQLite corpus
(``database/database.db``) when that file exists, otherwise from a
deterministic synthetic sample — so the suite stays self-contained on CI where
the gitignored corpus is absent.
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

# All application tables, in FK-safe DELETE order (children first). The
# separation from TRUNCATE matters: TRUNCATE costs ~2s per call on slow
# filesystems, DELETE is ~30x faster with the same observable result.
_ALL_TABLES = (
    "novel_search", "failed_novel", "tag_preference", "tag_alias",
    "task_history", "search_history", "scheduled_task", "token",
    "setting", "novel", "tag", "series", "author",
)

# Identity-sequence backers reset alongside cleanup (Tag, TagAlias, Token,
# ScheduledTask, TaskHistory, SearchHistory, TagPreference).
_SEQUENCE_TABLES = (
    "tag", "tag_alias", "token", "scheduled_task",
    "task_history", "search_history", "tag_preference",
)


def _clean_all_tables(conn) -> None:
    """Empty all application tables and restart identity sequences (fast)."""
    for table in _ALL_TABLES:
        conn.execute(text(f"DELETE FROM {table}"))
    for table in _SEQUENCE_TABLES:
        conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 1, false)"
        ))


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
    """Empty all application tables, restarting identity sequences.

    Uses DELETE (child-first) + ``setval`` instead of ``TRUNCATE ... RESTART
    IDENTITY``: TRUNCATE costs ~2s per test here (slow filesystem), DELETE is
    ~30x faster and keeps the same "fresh empty DB" semantics.
    """
    with pg_engine.begin() as conn:
        _clean_all_tables(conn)
    yield


@pytest.fixture(scope="session")
def seeded_db(pg_engine):
    """Seed the test DB with a 200-novel sample.

    Uses the legacy SQLite corpus (``database/database.db``) when present so
    local runs keep the real-data smoke check; on CI / machines without the
    corpus it falls back to a deterministic synthetic sample (see
    ``_seed_synthetic_sample``), keeping the suite self-contained.
    """
    sqlite_src = PROJECT_ROOT / "database" / "database.db"
    if sqlite_src.exists():
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "migrate_sqlite_to_pg.py"),
             "--limit", "200", "--db", TEST_DB, "--reset"],
            check=True,
        )
    else:
        _seed_synthetic_sample(pg_engine)
    yield pg_engine


def _seed_synthetic_sample(pg_engine) -> None:
    """Deterministic 200-novel sample inserted directly into PostgreSQL.

    Mirrors what the migration script produces: authors + novels with tags
    (tag rows/reference counts come from the statement-level triggers in
    migrations 0001/0002) and char-gram ``novel_search`` rows, so the
    repo-smoke assertions (pagination, R-18 exclusion, keyword:催, like-sort)
    have the same invariants to check.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session

    from copixiv.db.models import Author, Novel, NovelSearch
    from copixiv.features.novels.fts import build_search_text

    with pg_engine.begin() as conn:
        _clean_all_tables(conn)

    epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with Session(pg_engine) as s:
        authors, novels, searches = [], [], []
        for i in range(1, 201):
            author_id = 900_000_000 + i
            author_name = f"作者{i:03d}"
            # One title carries 催 so the single-quote keyword test has a hit;
            # the tag mix keeps R-18 / 日常 / 中文 / 催眠 reference counts > 0.
            title = "催眠の誘い" if i == 1 else f"合成小说{i:03d}"
            if i % 4 == 0:
                tags = ["R-18", "日常", "中文"]
            elif i % 5 == 0:
                tags = ["R-18"]
            elif i % 7 == 0:
                tags = ["催眠"]
            else:
                tags = ["日常", "中文"]
            like = 100 + i * 37
            authors.append(Author(author_id=author_id, author_name=author_name))
            novels.append(Novel(
                id=i, title=title, author_id=author_id, author_name=author_name,
                like=like, view=like * 10, text=like * 3, shuffle=i,
                create_time=epoch + timedelta(hours=i),
                tags=tags, is_favourite=(i % 17 == 0),
            ))
            searches.append(NovelSearch(
                novel_id=i,
                search_text=build_search_text(title, author_name, None, tags),
            ))
        s.add_all(authors)
        s.flush()
        s.add_all(novels)
        s.flush()
        s.add_all(searches)
        s.commit()
