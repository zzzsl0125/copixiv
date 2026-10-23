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
    "author", "series", "novel", "tag", "tag_alias",
    "tag_preference", "setting", "scheduled_task", "task_history",
    "token", "failed_novel", "search_history", "series_dead_letter",
}

# Tables that the greenfield schema eliminated (replaced by novel.tags /
# novel.is_favourite etc.), plus the search derived table that migration 0003
# replaced with an expression index on ``novel``.
REMOVED_TABLES = {
    "novel_tag", "favourite", "special_follow", "novel_fts", "novel_search",
}


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

    def test_scheduled_task_dropped_dead_config_column(self, pg_engine):
        """0011 removed the never-read ``config`` TEXT column.

        The ORM model and the migrated schema must agree: keeping the column in
        one place only would make a downgrade (or a fresh install) differ from
        the running app, which is exactly the drift 0011 exists to remove.
        """
        insp = inspect(pg_engine)
        columns = {c["name"] for c in insp.get_columns("scheduled_task")}
        assert "config" not in columns
        assert {"id", "name", "task", "cron", "params", "is_enabled",
                "sort_index"} <= columns

        from copixiv.db.models import ScheduledTask

        assert "config" not in ScheduledTask.__table__.columns


    def test_hot_tables_analyze_on_a_nightly_write_volume(self, pg_engine):
        """0013 lowers the analyze thresholds for ``novel`` / ``tag``.

        Both carry statistics that decide *query plans* (``search_lower``'s
        bigram selectivity, ``tag.reference_count``'s ordering histogram), and
        the project runs no ``ANALYZE`` itself.  With PostgreSQL's defaults a
        246 k-row table is only re-analyzed after ~24.7 k rows change — several
        nights of ``novel_follow`` — so plans can run on stale statistics;
        ``docs/perf`` measured a 7.3 s plan degrading to 1.7 ms once analyzed.
        """
        reloptions = dict(pg_engine.connect().execute(text(
            "select relname, reloptions from pg_class "
            "where relname in ('novel','tag')"
        )).all())

        for table, scale in (("novel", 0.02), ("tag", 0.05)):
            opts = reloptions[table] or []
            assert any(
                o == f"autovacuum_analyze_scale_factor={scale}" for o in opts
            ), f"{table} 的 analyze 阈值没被下调：{opts}"
            assert any(
                o.startswith("autovacuum_analyze_threshold=") for o in opts
            ), f"{table} 的 analyze threshold 没设置：{opts}"


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
        assert ({"novel", "author", "tag"} <= set(insp.get_table_names()))
        assert "novel_search" not in insp.get_table_names()
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == _head_revision()
    finally:
        engine.dispose()


@pytest.mark.slow
def test_jsonb_string_rows_are_repaired_to_objects(tmp_path):
    """0015 rewrites the ``jsonb_typeof='string'`` rows into objects.

    Regression for the ``recover_stale`` bug: it stored ``json.dumps`` output
    into a JSONB column, so ``result->>'summary'`` was NULL and the UI showed an
    empty summary.  The writer is fixed; this migration repairs the rows that
    were already written.

    Runs on a scratch database so it can seed the corrupt rows before upgrading
    past 0014.  Verifies both halves: a decodable object is unwrapped, and a
    string that is *not* an object is left alone (that is history, not encoding).
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    db = "copixiv_jsonb_repair_test"
    url = f"postgresql+psycopg2://postgres@127.0.0.1:5433/{db}"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "pg_dev.py"),
         "createdb", db],
        check=True,
    )

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    # Land on the revision *before* the repair, then seed the corrupt shapes.
    # ``downgrade`` first: the scratch DB may already sit at head from an
    # earlier run, and ``upgrade`` to an old revision is a no-op from head.
    command.downgrade(cfg, "0014_task_history_retention")

    engine = create_engine(url)
    with engine.begin() as conn:
        # The scratch DB outlives one run; start from an empty table so the
        # seeded ids cannot collide on a rerun.
        conn.execute(text("DELETE FROM task_history"))
        conn.execute(text("""
            INSERT INTO task_history (id, name, status, start_time, result)
            VALUES
              (1, 'legacy', 'interrupted', now(),
               '"{\\"summary\\": \\"服务重启导致中断\\"}"'::jsonb),
              (2, 'plain-log', 'success', now(), '"just text"'::jsonb),
              (3, 'healthy', 'success', now(), '{"summary": "ok"}'::jsonb)
        """))

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = dict(conn.execute(text(
            "select id, result from task_history"
        )).all())
        kinds = dict(conn.execute(text(
            "select id, jsonb_typeof(result) from task_history"
        )).all())

    assert kinds[1] == "object", "可还原的字符串应变成对象"
    assert rows[1] == {"summary": "服务重启导致中断"}
    assert kinds[2] == "string", "不可还原的字符串保持原样（那是历史内容）"
    assert kinds[3] == "object", "本来就正常的行不受影响"
    engine.dispose()
