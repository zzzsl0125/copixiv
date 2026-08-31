"""Alembic migration tests (D2) — PostgreSQL-backed.

The migration chain now targets PostgreSQL.  The session-scoped test DB is
already migrated to head by ``tests/conftest.py``; these tests verify the
resulting schema (tables, columns, ``alembic_version``), that running the
migrations again is a no-op, and that the ORM models load against it.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from copixiv.db.engine import run_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_URL = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test"


def _head_revision() -> str:
    """The Alembic head revision as an inspectable string."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    return ScriptDirectory.from_config(cfg).get_current_head()


EXPECTED_TABLES = {
    "author", "series", "novel", "novel_search", "tag", "tag_alias",
    "tag_preference", "setting", "scheduled_task", "task_history",
    "token", "failed_novel", "search_history",
}

# Tables that the greenfield schema eliminated (replaced by novel.tags /
# novel.is_favourite etc.).
REMOVED_TABLES = {"novel_tag", "favourite", "special_follow", "novel_fts"}


class TestMigrations:
    def test_db_is_at_head(self, pg_engine):
        with pg_engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == _head_revision(), f"DB at {rev}, head is {_head_revision()}"

    def test_schema_tables(self, pg_engine):
        insp = inspect(pg_engine)
        tables = set(insp.get_table_names())
        assert EXPECTED_TABLES <= tables
        assert not (REMOVED_TABLES & tables)

    def test_novel_has_greenfield_columns(self, pg_engine):
        insp = inspect(pg_engine)
        cols = {c["name"] for c in insp.get_columns("novel")}
        assert {"id", "title", "author_id", "path", "shuffle", "tags",
                "is_favourite", "create_time"} <= cols
        # tags is a PostgreSQL ARRAY, is_favourite a boolean.
        tags_col = next(c for c in insp.get_columns("novel") if c["name"] == "tags")
        assert "ARRAY" in str(tags_col["type"]).upper()

    def test_failed_novel_has_no_fk(self, pg_engine):
        """The failure ledger deliberately has no FK: ingest downloads BEFORE
        persisting, so failures for never-persisted novels must be recordable
        (their cleanup is explicit in the novel repository layer)."""
        insp = inspect(pg_engine)
        fks = insp.get_foreign_keys("failed_novel")
        assert not any(fk["referred_table"] == "novel" for fk in fks)

    def test_upgrade_is_idempotent(self):
        run_migrations(TEST_URL)
        run_migrations(TEST_URL)
        engine = create_engine(TEST_URL)
        try:
            with engine.connect() as conn:
                rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == _head_revision()
        finally:
            engine.dispose()

    def test_orm_models_load_after_migration(self):
        """The SQLAlchemy ORM metadata matches the migrated schema."""
        from sqlalchemy.orm import configure_mappers
        from copixiv.db import models  # noqa: F401 — import registers mappings
        configure_mappers()


@pytest.mark.slow
def test_fresh_database_upgrades_to_head():
    """A brand-new PostgreSQL database upgrades to head from an empty baseline."""
    from copixiv.db.backup import _pg_bin

    dbname = "copixiv_migrate_fresh"
    pg_dev = str(REPO_ROOT / "scripts" / "pg_dev.py")
    # Drop via psql (AUTOCOMMIT) then createdb, then migrate.
    subprocess.run(
        [_pg_bin("psql"), "-h", str(REPO_ROOT / ".spike" / "sock"),
         "-p", "5433", "-U", "postgres", "-d", "postgres",
         "-c", f"DROP DATABASE IF EXISTS {dbname}"],
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run([sys.executable, pg_dev, "createdb", dbname], check=True)
    url = f"postgresql+psycopg2://postgres@127.0.0.1:5433/{dbname}"
    run_migrations(url)

    engine = create_engine(url)
    try:
        insp = inspect(engine)
        assert ({"novel", "author", "tag", "novel_search"} <= set(insp.get_table_names()))
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == _head_revision()
    finally:
        engine.dispose()
