"""Layer 2 DB tests: squashed 0001_baseline roundtrip (upgrade/downgrade/upgrade) & drift alignment."""

from __future__ import annotations

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
            assert ver == "0005_align_schema_drift"

            # ORM Base.metadata parity (types & server defaults)
            mc = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diffs = compare_metadata(mc, Base.metadata)
            assert diffs == [], f"Unexpected metadata drift after upgrade head: {diffs!r}"

            args_type = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'task_history' "
                    "AND column_name = 'arguments'"
                ),
                {"s": schema},
            ).scalar_one()
            assert args_type == "jsonb"
            active_idx = conn.execute(
                text(
                    "SELECT COUNT(*) FROM pg_indexes "
                    "WHERE schemaname = :s AND indexname = 'ix_task_history_active_func'"
                ),
                {"s": schema},
            ).scalar_one()
            assert active_idx == 0

            # Custom sequence, analyze tuning, and tag triggers
            assert conn.execute(text("SELECT nextval('novel_local_coord_seq')")).scalar_one() == 1

            conn.execute(text("INSERT INTO author (author_id, author_name) VALUES (1, 'A')"))

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


def test_0005_align_schema_drift_fixes_legacy_default_and_constraint(pg_test_engine):
    schema = "_test_mig_0005_drift"
    with pg_test_engine.begin() as admin_conn:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))

    isolated_url = _isolated_schema_url(pg_test_engine.url, schema)
    isolated_engine = create_engine(isolated_url)
    alembic_cfg = _alembic_config_for_url(isolated_url)
    try:
        command.upgrade(alembic_cfg, "0004_novel_shuffle_and_indexes")

        # Simulate legacy drift: sequence default on series_dead_letter.series_id
        # and old auto-named check constraint on tag_preference.
        with isolated_engine.begin() as conn:
            conn.execute(text("CREATE SEQUENCE series_dead_letter_series_id_seq"))
            conn.execute(
                text(
                    "ALTER TABLE series_dead_letter "
                    "ALTER COLUMN series_id SET DEFAULT nextval('series_dead_letter_series_id_seq')"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE tag_preference "
                    "RENAME CONSTRAINT ck_tag_preference_value TO tag_preference_preference_check"
                )
            )

        command.upgrade(alembic_cfg, "head")

        with isolated_engine.begin() as conn:
            col_default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'series_dead_letter' "
                    "AND column_name = 'series_id'"
                ),
                {"s": schema},
            ).scalar_one()
            assert col_default is None
            assert (
                conn.execute(text("SELECT to_regclass('series_dead_letter_series_id_seq')")).scalar() is None
            )

            constraints = set(
                conn.execute(
                    text(
                        "SELECT c.conname FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "WHERE n.nspname = :s AND t.relname = 'tag_preference'"
                    ),
                    {"s": schema},
                ).scalars()
            )
            assert "ck_tag_preference_value" in constraints
            assert "tag_preference_preference_check" not in constraints
    finally:
        isolated_engine.dispose()
        with pg_test_engine.begin() as admin_conn:
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
