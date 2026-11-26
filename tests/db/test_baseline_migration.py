"""Layer 2 DB tests: Alembic baseline roundtrip (upgrade/downgrade/upgrade) & schema migrations."""

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
            assert ver == "0007_multi_source_foundation"

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

            # tag table, tag triggers, and sync_tag_refs_batch are dropped at head
            assert conn.execute(text("SELECT to_regclass('tag')")).scalar() is None
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
            assert (
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_trigger tg "
                        "JOIN pg_class c ON c.oid = tg.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :s AND c.relname = 'novel' AND NOT tg.tgisinternal"
                    ),
                    {"s": schema},
                ).scalar_one()
                == 0
            )

            # tag_alias columns are TEXT with expected constraints & index
            alias_cols = dict(
                conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = 'tag_alias'"
                    ),
                    {"s": schema},
                ).all()
            )
            assert alias_cols == {"id": "bigint", "source": "text", "target": "text"}
            alias_constraints = set(
                conn.execute(
                    text(
                        "SELECT c.conname FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "WHERE n.nspname = :s AND t.relname = 'tag_alias'"
                    ),
                    {"s": schema},
                ).scalars()
            )
            assert {"tag_alias_pkey", "tag_alias_source_key", "ck_tag_alias_distinct"} <= alias_constraints

            # Custom sequences, analyze tuning, and basic novel writes without tag table
            assert conn.execute(text("SELECT nextval('novel_local_coord_seq')")).scalar_one() == 1
            assert conn.execute(text("SELECT nextval('author_ext_id_seq')")).scalar_one() == 1
            assert conn.execute(text("SELECT nextval('series_ext_id_seq')")).scalar_one() == 1

            conn.execute(text("INSERT INTO author (author_id, author_name, source_key) VALUES (1, 'A', '1')"))
            conn.execute(
                text(
                    "INSERT INTO novel (id, title, author_id, tags, source_key) "
                    "VALUES (1, 'N1', 1, ARRAY['t1', 't2'], '1')"
                )
            )
            conn.execute(text("UPDATE novel SET tags = ARRAY['t2', 't3'] WHERE id = 1"))
            conn.execute(text("DELETE FROM novel WHERE id = 1"))
            conn.execute(
                text(
                    "INSERT INTO source_credential (source, name, kind, secret, meta) "
                    "VALUES ('pixiv', 'acc1', 'refresh_token', '{\"refresh_token\":\"tok1\"}'::jsonb, "
                    "'{\"is_follow\": true}'::jsonb)"
                )
            )
            conn.execute(text("INSERT INTO tag_alias (source, target) VALUES ('src_a', 'tgt_a')"))

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

        # Unique & distinct constraints on tag_alias and ux_source_credential_pixiv_follow
        with pytest.raises(IntegrityError), isolated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO source_credential (source, name, kind, secret, meta) "
                    "VALUES ('pixiv', 'acc2', 'refresh_token', '{\"refresh_token\":\"tok2\"}'::jsonb, "
                    "'{\"is_follow\": true}'::jsonb)"
                )
            )
        with pytest.raises(IntegrityError), isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO tag_alias (source, target) VALUES ('src_a', 'other')"))
        with pytest.raises(IntegrityError), isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO tag_alias (source, target) VALUES ('same', 'same')"))

        # 2) Downgrade back to base drops all tables, triggers, function, and sequences
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
            assert conn.execute(text("SELECT to_regclass('author_ext_id_seq')")).scalar() is None
            assert conn.execute(text("SELECT to_regclass('series_ext_id_seq')")).scalar() is None
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
            conn.execute(
                text(
                    "INSERT INTO novel (id, title, tags, source_key) VALUES (2, 'N2', ARRAY['rebuilt'], '2')"
                )
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


def test_0006_drop_tag_table_preserves_aliases_and_repopulates_on_downgrade(pg_test_engine):
    schema = "_test_mig_0006_roundtrip"
    with pg_test_engine.begin() as admin_conn:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))

    isolated_url = _isolated_schema_url(pg_test_engine.url, schema)
    isolated_engine = create_engine(isolated_url)
    alembic_cfg = _alembic_config_for_url(isolated_url)
    try:
        command.upgrade(alembic_cfg, "0005_align_schema_drift")

        with isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO author (author_id, author_name) VALUES (1, 'A')"))
            conn.execute(
                text(
                    "INSERT INTO novel (id, title, author_id, tags) VALUES "
                    "(1, 'N1', 1, ARRAY['alpha', 'beta']), "
                    "(2, 'N2', 1, ARRAY['alpha', 'gamma'])"
                )
            )
            conn.execute(text("INSERT INTO tag (name, reference_count) VALUES ('orphan_src', 0)"))
            conn.execute(
                text(
                    "INSERT INTO tag_alias (source, target) "
                    "SELECT s.id, t.id FROM tag s, tag t WHERE s.name = 'beta' AND t.name = 'alpha'"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO tag_alias (source, target) "
                    "SELECT s.id, t.id FROM tag s, tag t WHERE s.name = 'orphan_src' AND t.name = 'alpha'"
                )
            )

        # Upgrade 0005 -> 0006 (head)
        command.upgrade(alembic_cfg, "head")
        with isolated_engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('tag')")).scalar() is None
            alias_rows = set(conn.execute(text("SELECT source, target FROM tag_alias ORDER BY id")).all())
            assert alias_rows == {("beta", "alpha"), ("orphan_src", "alpha")}
            mc = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            assert compare_metadata(mc, Base.metadata) == []

        # Downgrade 0006 -> 0005 restores tag table counts, tag_alias FKs, and triggers
        command.downgrade(alembic_cfg, "0005_align_schema_drift")
        with isolated_engine.begin() as conn:
            tag_counts = dict(conn.execute(text("SELECT name, reference_count FROM tag")).all())
            assert tag_counts == {"alpha": 2, "beta": 1, "gamma": 1, "orphan_src": 0}
            restored_aliases = set(
                conn.execute(
                    text(
                        "SELECT s.name, t.name "
                        "FROM tag_alias ta "
                        "JOIN tag s ON s.id = ta.source "
                        "JOIN tag t ON t.id = ta.target"
                    )
                ).all()
            )
            assert restored_aliases == {("beta", "alpha"), ("orphan_src", "alpha")}

            # Reinstalled triggers update reference_count on novel insert
            conn.execute(
                text(
                    "INSERT INTO novel (id, title, author_id, tags) VALUES (3, 'N3', 1, ARRAY['alpha', 'delta'])"
                )
            )
            assert (
                conn.execute(text("SELECT reference_count FROM tag WHERE name = 'alpha'")).scalar_one() == 3
            )
            assert (
                conn.execute(text("SELECT reference_count FROM tag WHERE name = 'delta'")).scalar_one() == 1
            )

        # Re-upgrade 0005 -> head
        command.upgrade(alembic_cfg, "head")
        with isolated_engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('tag')")).scalar() is None
            alias_rows = set(conn.execute(text("SELECT source, target FROM tag_alias ORDER BY id")).all())
            assert alias_rows == {("beta", "alpha"), ("orphan_src", "alpha")}
    finally:
        isolated_engine.dispose()
        with pg_test_engine.begin() as admin_conn:
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


def test_0007_multi_source_roundtrip(pg_test_engine):
    schema = "_test_mig_0007_roundtrip"
    with pg_test_engine.begin() as admin_conn:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))

    isolated_url = _isolated_schema_url(pg_test_engine.url, schema)
    isolated_engine = create_engine(isolated_url)
    alembic_cfg = _alembic_config_for_url(isolated_url)
    try:
        command.upgrade(alembic_cfg, "0006_drop_tag_table")

        with isolated_engine.begin() as conn:
            conn.execute(text("INSERT INTO author (author_id, author_name) VALUES (1, 'A1')"))
            conn.execute(text("INSERT INTO series (series_id, series_name, author_id) VALUES (10, 'S10', 1)"))
            conn.execute(
                text(
                    "INSERT INTO novel (id, title, author_id, series_id, source, source_key) VALUES "
                    "(100, 'P100', 1, 10, 'pixiv', NULL), "
                    "(-1, 'L1', NULL, NULL, 'local', 'sha_l1')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO failed_novel (novel_id, error_message, failed_times) VALUES (200, 'err', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO token (name, token, premium, valid, sort_index, is_follow) VALUES "
                    "('acc1', 'tok_1', false, true, 0, false), "
                    "('acc2', 'tok_2', true, true, 1, true)"
                )
            )

        # Upgrade 0006 -> 0007 (head)
        command.upgrade(alembic_cfg, "head")
        with isolated_engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('token')")).scalar() is None
            assert conn.execute(text("SELECT source_key FROM author WHERE author_id = 1")).scalar_one() == "1"
            assert (
                conn.execute(text("SELECT source_key FROM series WHERE series_id = 10")).scalar_one() == "10"
            )
            assert conn.execute(text("SELECT source_key FROM novel WHERE id = 100")).scalar_one() == "100"
            assert conn.execute(text("SELECT source_key FROM novel WHERE id = -1")).scalar_one() == "sha_l1"

            creds = conn.execute(
                text(
                    "SELECT name, kind, secret->>'refresh_token', (meta->>'premium')::boolean, "
                    "(meta->>'is_follow')::boolean "
                    "FROM source_credential WHERE source = 'pixiv' ORDER BY sort_index"
                )
            ).all()
            assert creds == [
                ("acc1", "refresh_token", "tok_1", False, False),
                ("acc2", "refresh_token", "tok_2", True, True),
            ]
            assert conn.execute(text("SELECT nextval('author_ext_id_seq')")).scalar_one() == 1
            assert conn.execute(text("SELECT nextval('series_ext_id_seq')")).scalar_one() == 1

            # Verify non-pixiv failure row without novel_id is accepted at 0007
            conn.execute(
                text(
                    "INSERT INTO failed_novel (source, source_key, novel_id, error_message) "
                    "VALUES ('esj', 'book1:1', NULL, 'blocked')"
                )
            )

            mc = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            assert compare_metadata(mc, Base.metadata) == []

        # Downgrade 0007 -> 0006
        command.downgrade(alembic_cfg, "0006_drop_tag_table")
        with isolated_engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('source_credential')")).scalar() is None
            assert conn.execute(text("SELECT to_regclass('source_config')")).scalar() is None
            restored_tokens = conn.execute(
                text("SELECT name, token, premium, is_follow FROM token ORDER BY sort_index")
            ).all()
            assert restored_tokens == [("acc1", "tok_1", False, False), ("acc2", "tok_2", True, True)]
            failed_rows = conn.execute(text("SELECT novel_id FROM failed_novel")).scalars().all()
            assert failed_rows == [200]

        # Re-upgrade 0006 -> head
        command.upgrade(alembic_cfg, "head")
    finally:
        isolated_engine.dispose()
        with pg_test_engine.begin() as admin_conn:
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
