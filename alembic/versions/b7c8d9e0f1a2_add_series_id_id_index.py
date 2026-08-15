"""add_series_id_id_index

Revision ID: b7c8d9e0f1a2
Revises: 9ea7477570f8
Create Date: 2026-08-15 18:00:00.000000

Add composite index idx_novel_series_id on novel(series_id, id) so
series-filtered queries ordered by novel.id can avoid a TEMP B-TREE.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = '9ea7477570f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the series_id + id index."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_novel_series_id "
        "ON novel (series_id, id)"
    )


def downgrade() -> None:
    """Drop the series_id + id index."""
    op.execute("DROP INDEX IF EXISTS idx_novel_series_id")
