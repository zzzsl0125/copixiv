"""Layer 2 DB tests: squashed 0001_baseline roundtrip (upgrade/downgrade/upgrade) & prod transition."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import Base

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TRANSITION_SCRIPT = _PROJECT_ROOT / "scripts" / "transition_to_baseline.py"


def _load_transition_module():
    spec = importlib.util.spec_from_file_location("transition_to_baseline", _TRANSITION_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _isolated_schema_url(base_url, schema: str) -> str:
    return base_url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False
    )


def _alembic_config_for_url(db_url: str) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    return cfg


def test_baseline_upgrade_downgrade_upgrade_and_metadata_parity(pg_test_engine):
    schema = "_test_mig_baseline"
    with pg_test_engine.begin() as admin_conn:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))

    isolated_url = _isolated_schema_url(pg_test_engine.url, schema)
    isolated_engine = create_engine(isolated_url)
    alembic_cfg = _alembic_config_for_url(isolated_url)
    try:
        # 1) Empty schema -> upgrade head
        command.upgrade(alembic_cfg, "head")

        with isolated_engine.begin() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0001_baseline"

            # ORM Base.metadata parity (types & server defaults)
            mc = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diffs = compare_metadata(mc, Base.metadata)
            assert diffs == [], f"Unexpected metadata drift after 0001_baseline: {diffs!r}"

            # Custom sequence, BigInteger summary range, analyze tuning, and tag triggers
            assert conn.execute(text("SELECT nextval('novel_local_coord_seq')")).scalar_one() == 1

            conn.execute(
                text("INSERT INTO author (author_id, author_name, \"like\") VALUES (1, 'A', 5000000000)")
            )
            assert (
                conn.execute(text('SELECT "like" FROM author WHERE author_id = 1')).scalar_one()
                == 5_000_000_000
            )

            conn.execute(
                text("INSERT INTO novel (id, title, author_id, tags) VALUES (1, 'N1', 1, ARRAY['t1', 't2'])")
            )
            assert conn.execute(text("SELECT reference_count FROM tag WHERE name = 't1'")).scalar_one() == 1

            conn.execute(text("UPDATE novel SET tags = ARRAY['t2', 't3'] WHERE id = 1"))
            assert conn.execute(text("SELECT reference_count FROM tag WHERE name = 't1'")).scalar_one() == 0
            assert conn.execute(text("SELECT reference_count FROM tag WHERE name = 't2'")).scalar_one() == 1
            assert conn.execute(text("SELECT reference_count FROM tag WHERE name = 't3'")).scalar_one() == 1

            conn.execute(text("DELETE FROM novel WHERE id = 1"))
            assert conn.execute(text("SELECT reference_count FROM tag WHERE name = 't3'")).scalar_one() == 0

            conn.execute(text("INSERT INTO token (name, token, is_follow) VALUES ('acc1', 'tok1', true)"))

            novel_relopts = conn.execute(
                text(
                    "SELECT c.reloptions FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :s AND c.relname = 'novel'"
                ),
                {"s": schema},
            ).scalar_one()
            assert novel_relopts is not None
            assert any("autovacuum_analyze_scale_factor=0.02" in opt for opt in novel_relopts)

        # Partial unique index ux_token_follow blocks second is_follow=True row
        with pytest.raises(IntegrityError), isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO token (name, token, is_follow) VALUES ('acc2', 'tok2', true)"))

        # 2) Downgrade back to base drops all tables, triggers, function, and sequence
        command.downgrade(alembic_cfg, "base")

        with isolated_engine.connect() as conn:
            remaining_tables = (
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :s AND table_name <> 'alembic_version'"
                    ),
                    {"s": schema},
                )
                .scalars()
                .all()
            )
            assert remaining_tables == []
            assert conn.execute(text("SELECT to_regclass('novel_local_coord_seq')")).scalar() is None
            assert (
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = :s AND p.proname = 'sync_tag_refs_batch'"
                    ),
                    {"s": schema},
                ).scalar_one()
                == 0
            )

        # 3) Second upgrade head cleanly restores the schema
        command.upgrade(alembic_cfg, "head")
        with isolated_engine.begin() as conn:
            mc = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            assert compare_metadata(mc, Base.metadata) == []
            conn.execute(text("INSERT INTO novel (id, title, tags) VALUES (2, 'N2', ARRAY['rebuilt'])"))
            assert (
                conn.execute(text("SELECT reference_count FROM tag WHERE name = 'rebuilt'")).scalar_one() == 1
            )
    finally:
        isolated_engine.dispose()
        with pg_test_engine.begin() as admin_conn:
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


def test_transition_to_baseline_from_legacy_0016_schema(pg_test_engine):
    schema = "_test_mig_transition"
    mod = _load_transition_module()

    with pg_test_engine.begin() as admin_conn:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))

    isolated_url = _isolated_schema_url(pg_test_engine.url, schema)
    isolated_engine = create_engine(isolated_url)
    try:
        # Build a legacy 0016-shaped schema (with search_lower, INTEGER counters, legacy indexes)
        with isolated_engine.begin() as conn:
            conn.execute(
                text(
                    """
                CREATE FUNCTION copixiv_search_text(t text, a text, s text, tags text[])
                RETURNS text LANGUAGE sql IMMUTABLE AS $$
                  SELECT lower(concat_ws(' ', nullif(t, ''), nullif(a, ''), nullif(s, ''), nullif(array_to_string(tags, ' '), '')))
                $$;
                CREATE TABLE author (
                    author_id BIGINT PRIMARY KEY,
                    author_name TEXT,
                    novel_count INTEGER NOT NULL DEFAULT 0,
                    "like" INTEGER NOT NULL DEFAULT 0,
                    view INTEGER NOT NULL DEFAULT 0,
                    text INTEGER NOT NULL DEFAULT 0,
                    last_update TIMESTAMPTZ,
                    is_special_follow BOOLEAN NOT NULL DEFAULT false
                );
                CREATE INDEX ix_author_special_follow ON author (author_id) WHERE is_special_follow;
                CREATE INDEX ix_author_last_update ON author (last_update);

                CREATE TABLE series (
                    series_id BIGINT PRIMARY KEY,
                    series_name TEXT,
                    novel_count INTEGER NOT NULL DEFAULT 0,
                    author_id BIGINT REFERENCES author(author_id) ON DELETE SET NULL,
                    "like" INTEGER NOT NULL DEFAULT 0,
                    view INTEGER NOT NULL DEFAULT 0,
                    text INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX ix_series_author_id ON series (author_id);

                CREATE TABLE novel (
                    id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author_id BIGINT REFERENCES author(author_id),
                    author_name TEXT,
                    path TEXT UNIQUE,
                    "like" INTEGER NOT NULL DEFAULT 0,
                    view INTEGER NOT NULL DEFAULT 0,
                    text INTEGER NOT NULL DEFAULT 0,
                    caption TEXT,
                    series_id BIGINT REFERENCES series(series_id) ON DELETE SET NULL,
                    series_name TEXT,
                    series_index INTEGER,
                    create_time TIMESTAMPTZ,
                    has_epub INTEGER NOT NULL DEFAULT 0,
                    shuffle INTEGER NOT NULL DEFAULT 0,
                    tags TEXT[] NOT NULL DEFAULT '{}'::text[],
                    is_favourite BOOLEAN NOT NULL DEFAULT false,
                    source TEXT NOT NULL DEFAULT 'pixiv'::text,
                    source_key TEXT,
                    search_lower TEXT GENERATED ALWAYS AS (copixiv_search_text(title, author_name, series_name, tags)) STORED
                );
                CREATE INDEX ix_novel_like_text_id ON novel ("like", text, id);
                CREATE INDEX ix_novel_like_id ON novel ("like", id);
                CREATE INDEX ix_novel_shuffle_id ON novel (shuffle, id);
                CREATE INDEX ix_novel_shuffle_like_text ON novel (shuffle, "like", text);
                CREATE INDEX ix_novel_author_id ON novel (author_id, id);
                CREATE INDEX ix_novel_series_id ON novel (series_id, id);
                CREATE INDEX ix_novel_author_like ON novel (author_id, "like");
                CREATE INDEX ix_novel_series_like ON novel (series_id, "like");
                CREATE INDEX ix_novel_create_time ON novel (create_time);
                CREATE INDEX ix_novel_tags_gin ON novel USING gin (tags);
                CREATE INDEX ix_novel_source ON novel (source);
                CREATE UNIQUE INDEX ux_novel_source_key ON novel (source, source_key) WHERE source_key IS NOT NULL;
                CREATE INDEX ix_novel_favourite ON novel (id) WHERE is_favourite;
                CREATE INDEX ix_novel_abs_id ON novel (abs(id), id);

                CREATE TABLE tag (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    reference_count INTEGER NOT NULL DEFAULT 0,
                    search_lower TEXT GENERATED ALWAYS AS (lower(name)) STORED
                );
                CREATE INDEX tag_reference_count_id_idx ON tag (reference_count DESC, id DESC);

                CREATE TABLE tag_preference (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    tag TEXT NOT NULL UNIQUE,
                    preference TEXT NOT NULL,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT ck_tag_preference_value CHECK (preference IN ('favourite', 'blocked'))
                );
                CREATE TABLE setting (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE tag_alias (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    source BIGINT NOT NULL UNIQUE REFERENCES tag(id),
                    target BIGINT NOT NULL REFERENCES tag(id)
                );
                CREATE INDEX ix_tag_alias_target ON tag_alias (target);
                CREATE TABLE scheduled_task (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL,
                    task TEXT NOT NULL,
                    cron TEXT NOT NULL,
                    params JSONB,
                    is_enabled BOOLEAN NOT NULL DEFAULT false,
                    sort_index INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE task_history (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL,
                    task_func TEXT,
                    arguments TEXT,
                    status TEXT NOT NULL,
                    start_time TIMESTAMPTZ NOT NULL,
                    end_time TIMESTAMPTZ,
                    duration DOUBLE PRECISION,
                    result JSONB,
                    progress JSONB
                );
                CREATE INDEX ix_task_history_pending ON task_history (id) WHERE status = 'pending';
                CREATE INDEX ix_task_history_active_func ON task_history (task_func) WHERE status IN ('pending', 'running');
                CREATE TABLE token (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    token TEXT NOT NULL,
                    premium BOOLEAN NOT NULL DEFAULT false,
                    valid BOOLEAN NOT NULL DEFAULT true,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    is_follow BOOLEAN NOT NULL DEFAULT false
                );
                CREATE TABLE failed_novel (
                    novel_id BIGINT PRIMARY KEY,
                    failure_type TEXT,
                    error_message TEXT,
                    failed_times INTEGER NOT NULL DEFAULT 1,
                    title TEXT,
                    last_failed_at TIMESTAMPTZ
                );
                CREATE INDEX ix_failed_novel_last_failed_at ON failed_novel (last_failed_at);
                CREATE TABLE series_dead_letter (
                    series_id BIGINT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_failed_at TIMESTAMPTZ,
                    retired_at TIMESTAMPTZ
                );
                CREATE INDEX ix_series_dead_letter_retired_at ON series_dead_letter (retired_at) WHERE retired_at IS NOT NULL;
                CREATE TABLE search_history (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    display_value TEXT,
                    "timestamp" TIMESTAMPTZ NOT NULL,
                    CONSTRAINT uq_search_history_type_value UNIQUE (type, value)
                );
                CREATE INDEX ix_search_history_type_timestamp ON search_history (type, "timestamp");
                CREATE TABLE alembic_version (
                    version_num VARCHAR(32) NOT NULL CONSTRAINT alembic_version_pkc PRIMARY KEY
                );
                INSERT INTO alembic_version (version_num) VALUES ('0016_tag_refs_no_temp_table');
                INSERT INTO author (author_id, author_name, "like") VALUES (1, 'A', 123);
                INSERT INTO novel (id, title, author_id, tags) VALUES (1, 'N1', 1, ARRAY['t1']);
                INSERT INTO token (name, token, is_follow) VALUES ('acc1', 'tok1', true);
                """
                )
            )

        before = mod.inspect_transition_state(isolated_engine, schema=schema)
        assert before["alembic_versions"] == ["0016_tag_refs_no_temp_table"]
        assert "novel.search_lower" in before["search_lower_columns"]
        assert "ix_novel_abs_id" in before["legacy_indexes_present"]
        assert before["non_bigint_summary_columns"]

        after = mod.apply_baseline_transition(isolated_engine, isolated_url, schema=schema)
        assert after["alembic_versions"] == ["0001_baseline"]
        assert after["search_lower_columns"] == []
        assert after["legacy_indexes_present"] == []
        assert after["non_bigint_summary_columns"] == []
        assert after["missing_partial_indexes"] == []
        assert after["metadata_diffs"] == []

        # Second invocation is a safe no-op
        repeat = mod.apply_baseline_transition(isolated_engine, isolated_url, schema=schema)
        assert repeat["alembic_versions"] == ["0001_baseline"]
        assert repeat["metadata_diffs"] == []

        with isolated_engine.begin() as conn:
            conn.execute(text('UPDATE author SET "like" = 5000000000 WHERE author_id = 1'))
            conn.execute(text("INSERT INTO novel (id, title, tags) VALUES (2, 'N2', ARRAY['trig_a'])"))
            assert (
                conn.execute(text("SELECT reference_count FROM tag WHERE name = 'trig_a'")).scalar_one() == 1
            )

        with pytest.raises(IntegrityError), isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO token (name, token, is_follow) VALUES ('acc2', 'tok2', true)"))
    finally:
        isolated_engine.dispose()
        with pg_test_engine.begin() as admin_conn:
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
