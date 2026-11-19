"""Pre-squash production transition and Alembic baseline stamp helper.

Existing databases created on the old ``0001``–``0016`` or ``0017`` chain cannot
re-run ``0001_baseline`` (tables already exist). This tool inspects schema state,
idempotently completes any remaining ``0017`` cleanups (dropping ``pg_bigm``,
``search_lower``, legacy indexes, promoting ``author``/``series`` aggregate
columns to ``BIGINT``, creating maintenance partial indexes, and installing
``pg_objects``), verifies zero SQLAlchemy metadata drift, and stamps
``alembic_version`` to ``0001_baseline``.

Usage:
    uv run python scripts/transition_to_baseline.py             # read-only check
    uv run python scripts/transition_to_baseline.py --apply     # apply cleanup & stamp
    uv run python scripts/transition_to_baseline.py --url postgresql+psycopg2://... --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = str(_PROJECT_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from copixiv.config import load_config  # noqa: E402
from copixiv.db.models import Base  # noqa: E402
from copixiv.db.pg_objects import install_pg_objects  # noqa: E402

BASELINE_REVISION = "0001_baseline"

_DROPPED_LEGACY_INDEXES: tuple[str, ...] = (
    "novel_search_bigm",
    "tag_search_bigm",
    "ix_novel_like_text_id",
    "ix_novel_like_id",
    "ix_novel_shuffle_id",
    "ix_novel_shuffle_like_text",
    "ix_novel_author_like",
    "ix_novel_series_like",
    "ix_novel_create_time",
    "ix_novel_tags_gin",
    "ix_novel_source",
    "ix_novel_abs_id",
)

_EXPECTED_NEW_INDEXES: dict[str, str] = {
    "ix_novel_local_id": "CREATE INDEX IF NOT EXISTS ix_novel_local_id ON novel (id) WHERE id < 0",
    "ix_novel_missing_source": (
        "CREATE INDEX IF NOT EXISTS ix_novel_missing_source ON novel (id) WHERE source = ''"
    ),
    "ix_novel_series_missing_index": (
        "CREATE INDEX IF NOT EXISTS ix_novel_series_missing_index ON novel (series_id) "
        "WHERE series_id IS NOT NULL AND series_index IS NULL"
    ),
    "ix_novel_empty_author_name": (
        "CREATE INDEX IF NOT EXISTS ix_novel_empty_author_name ON novel (author_id) "
        "WHERE author_id IS NOT NULL AND (author_name IS NULL OR author_name = '')"
    ),
    "ux_token_follow": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_token_follow ON token (is_follow) WHERE is_follow"
    ),
}


def _resolve_alembic_config(database_url: str, project_root: Path = _PROJECT_ROOT) -> Config:
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def inspect_transition_state(engine: Engine, *, schema: str | None = None) -> dict[str, Any]:
    """Inspect schema objects, legacy artifacts, Alembic version, and ORM metadata parity."""
    with engine.connect() as conn:
        active_schema = schema or str(conn.execute(text("SELECT current_schema()")).scalar_one())
        tables = sorted(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                ),
                {"s": active_schema},
            )
            .scalars()
            .all()
        )
        has_novel = "novel" in tables

        alembic_versions: list[str] = []
        if "alembic_version" in tables:
            alembic_versions = list(
                conn.execute(text(f'SELECT version_num FROM "{active_schema}".alembic_version'))
                .scalars()
                .all()
            )

        has_pg_bigm = bool(
            conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm')")
            ).scalar()
        )

        search_lower_cols = sorted(
            f"{t}.{c}"
            for t, c in conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND column_name = 'search_lower'"
                ),
                {"s": active_schema},
            ).all()
        )

        existing_indexes = set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :s"),
                {"s": active_schema},
            )
            .scalars()
            .all()
        )
        legacy_indexes = [idx for idx in _DROPPED_LEGACY_INDEXES if idx in existing_indexes]
        missing_partial_indexes = [idx for idx in _EXPECTED_NEW_INDEXES if idx not in existing_indexes]

        non_bigint_summary_cols = sorted(
            f"{t}.{c}:{dt}"
            for t, c, dt in conn.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name IN ('author', 'series') "
                    "AND column_name IN ('like', 'view', 'text') AND data_type <> 'bigint'"
                ),
                {"s": active_schema},
            ).all()
        )

        metadata_diffs: list[str] = []
        if has_novel:
            mc = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            metadata_diffs = [repr(diff) for diff in compare_metadata(mc, Base.metadata)]

    return {
        "schema": active_schema,
        "has_novel_table": has_novel,
        "alembic_versions": alembic_versions,
        "has_pg_bigm": has_pg_bigm,
        "search_lower_columns": search_lower_cols,
        "legacy_indexes_present": legacy_indexes,
        "non_bigint_summary_columns": non_bigint_summary_cols,
        "missing_partial_indexes": missing_partial_indexes,
        "metadata_diffs": metadata_diffs,
    }


def apply_baseline_transition(
    engine: Engine,
    database_url: str,
    *,
    project_root: Path = _PROJECT_ROOT,
    schema: str | None = None,
) -> dict[str, Any]:
    """Idempotently align an existing database with 0001_baseline and stamp alembic_version."""
    before = inspect_transition_state(engine, schema=schema)
    if not before["has_novel_table"]:
        raise RuntimeError(
            "Target database has no copixiv tables; run `uv run alembic upgrade head` instead."
        )

    with engine.begin() as conn:
        for idx_name in _DROPPED_LEGACY_INDEXES:
            conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))

        conn.execute(text("ALTER TABLE novel DROP COLUMN IF EXISTS search_lower"))
        conn.execute(text("ALTER TABLE tag DROP COLUMN IF EXISTS search_lower"))
        conn.execute(text("DROP FUNCTION IF EXISTS copixiv_search_text(text, text, text, text[])"))
        conn.execute(text("DROP EXTENSION IF EXISTS pg_bigm"))

        if before["non_bigint_summary_columns"]:
            conn.execute(
                text(
                    'ALTER TABLE author ALTER COLUMN "like" TYPE BIGINT, '
                    "ALTER COLUMN view TYPE BIGINT, ALTER COLUMN text TYPE BIGINT"
                )
            )
            conn.execute(
                text(
                    'ALTER TABLE series ALTER COLUMN "like" TYPE BIGINT, '
                    "ALTER COLUMN view TYPE BIGINT, ALTER COLUMN text TYPE BIGINT"
                )
            )

        for ddl in _EXPECTED_NEW_INDEXES.values():
            conn.execute(text(ddl))

        install_pg_objects(conn)

        mc = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diffs = compare_metadata(mc, Base.metadata)
        if diffs:
            raise RuntimeError(f"Schema still diverges from ORM Base.metadata; refusing to stamp: {diffs!r}")

    alembic_cfg = _resolve_alembic_config(database_url, project_root=project_root)
    command.stamp(alembic_cfg, BASELINE_REVISION, purge=True)

    return inspect_transition_state(engine, schema=schema)


def _resolve_cli_url(explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url
    env_url = os.environ.get("COPIXIV_DATABASE_URL")
    if env_url:
        return env_url
    return load_config().database_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transition an existing copixiv DB to 0001_baseline.")
    parser.add_argument("--url", help="Target SQLAlchemy PostgreSQL URL.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply idempotent cleanup DDL, verify metadata parity, and stamp 0001_baseline.",
    )
    args = parser.parse_args(argv)

    database_url = _resolve_cli_url(args.url)
    engine = create_engine(database_url)
    try:
        if args.apply:
            state = apply_baseline_transition(engine, database_url)
            print("Transition applied and stamped cleanly:")
        else:
            state = inspect_transition_state(engine)
            print("Dry-run inspection (no changes applied):")

        for key, value in state.items():
            print(f"  {key}: {value!r}")

        if not args.apply and (
            state["alembic_versions"] != [BASELINE_REVISION]
            or state["metadata_diffs"]
            or state["legacy_indexes_present"]
            or state["search_lower_columns"]
            or state["non_bigint_summary_columns"]
            or state["missing_partial_indexes"]
            or state["has_pg_bigm"]
        ):
            print(
                "\nRe-run with --apply to complete baseline transition "
                "(or `uv run alembic upgrade head` if the DB is empty)."
            )
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
