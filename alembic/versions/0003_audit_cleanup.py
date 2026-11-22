"""Drop dead indexes, failed_novel.failure_type, and materialized author/series summaries.

Revision ID: 0003_audit_cleanup
Revises: 0002_simplify_task_history
Create Date: 2026-11-21 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_audit_cleanup"
down_revision: str | Sequence[str] | None = "0002_simplify_task_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED_INDEXES: tuple[str, ...] = (
    "tag_reference_count_id_idx",
    "ix_failed_novel_last_failed_at",
    "ix_novel_missing_source",
    "ix_novel_local_id",
    "ix_author_last_update",
    "ix_series_dead_letter_retired_at",
    "ix_novel_empty_author_name",
)

_SUMMARY_TABLES: tuple[str, ...] = ("author", "series")


def upgrade() -> None:
    for index_name in _DROPPED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    op.execute("ALTER TABLE failed_novel DROP COLUMN IF EXISTS failure_type")

    for table in _SUMMARY_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table}
            DROP COLUMN IF EXISTS novel_count,
            DROP COLUMN IF EXISTS "like",
            DROP COLUMN IF EXISTS "view",
            DROP COLUMN IF EXISTS "text"
            """
        )


def downgrade() -> None:
    for table in _SUMMARY_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS novel_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS "like" BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS "view" BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS "text" BIGINT NOT NULL DEFAULT 0
            """
        )

    op.execute("ALTER TABLE failed_novel ADD COLUMN IF NOT EXISTS failure_type TEXT")

    op.execute("CREATE INDEX ix_author_last_update ON author (last_update)")
    op.execute("CREATE INDEX ix_novel_local_id ON novel (id) WHERE id < 0")
    op.execute("CREATE INDEX ix_novel_missing_source ON novel (id) WHERE source = ''")
    op.execute(
        "CREATE INDEX ix_novel_empty_author_name ON novel (author_id) "
        "WHERE author_id IS NOT NULL AND (author_name IS NULL OR author_name = '')"
    )
    op.execute("CREATE INDEX tag_reference_count_id_idx ON tag (reference_count DESC, id DESC)")
    op.execute("CREATE INDEX ix_failed_novel_last_failed_at ON failed_novel (last_failed_at)")
    op.execute(
        "CREATE INDEX ix_series_dead_letter_retired_at ON series_dead_letter (retired_at) "
        "WHERE retired_at IS NOT NULL"
    )
