"""add_performance_indexes

Revision ID: 400fb7cc367d
Revises: af8d332686b1
Create Date: 2026-06-29 22:36:53.127694

Add performance indexes:
- ix_novel_create_time on novel(create_time)
- ix_search_history_type_timestamp on search_history(type, timestamp)
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = '400fb7cc367d'
down_revision: Union[str, Sequence[str], None] = 'af8d332686b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add performance indexes."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_novel_create_time ON novel (create_time)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_history_type_timestamp "
        "ON search_history (type, timestamp)"
    )


def downgrade() -> None:
    """Remove performance indexes."""
    op.execute("DROP INDEX IF EXISTS ix_novel_create_time")
    op.execute("DROP INDEX IF EXISTS ix_search_history_type_timestamp")
