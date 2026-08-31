"""add_author_id_index

Revision ID: a1b2c3d4e5f6
Revises: 400fb7cc367d
Create Date: 2026-06-30 12:00:00.000000

Add composite index idx_novel_author_id on novel(author_id, id)
to speed up special-follow queries that filter by author_id and
order by id.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '400fb7cc367d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_novel_author_id ON novel (author_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_novel_author_id")
