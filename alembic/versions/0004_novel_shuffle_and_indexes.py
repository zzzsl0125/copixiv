"""Drop novel.shuffle, unused indexes, stale search_history rows, and fix ix_novel_missing_source.

Revision ID: 0004_novel_shuffle_and_indexes
Revises: 0003_audit_cleanup
Create Date: 2026-11-21 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_novel_shuffle_and_indexes"
down_revision: str | Sequence[str] | None = "0003_audit_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_novel_favourite")
    op.execute("DROP INDEX IF EXISTS ix_search_history_type_timestamp")
    op.execute("DROP INDEX IF EXISTS ix_novel_missing_source")
    op.execute(
        "CREATE INDEX ix_novel_missing_source ON novel (id) "
        "WHERE has_epub = 4 AND source = 'pixiv' AND id > 0"
    )
    op.execute("ALTER TABLE novel DROP COLUMN IF EXISTS shuffle")
    op.execute(
        "DELETE FROM search_history WHERE type IN ('is_favourite', 'is_special_follow', 'source')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE novel ADD COLUMN IF NOT EXISTS shuffle INTEGER NOT NULL DEFAULT 0")
    op.execute("DROP INDEX IF EXISTS ix_novel_missing_source")
    op.execute("CREATE INDEX ix_novel_favourite ON novel (id) WHERE is_favourite")
    op.execute(
        'CREATE INDEX ix_search_history_type_timestamp ON search_history (type, "timestamp")'
    )
