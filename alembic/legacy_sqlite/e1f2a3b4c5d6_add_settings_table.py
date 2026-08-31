"""add_settings_table

Revision ID: e1f2a3b4c5d6
Revises: c8d9e0f1a2b3
Create Date: 2026-08-16 18:00:00.000000

Runtime key-value settings table for UI-changeable configuration
(e.g. exclude_blocked_tag_novels).  Separate from config.yaml.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'settings',
        sa.Column('key', sa.String(255), primary_key=True),
        sa.Column('value', sa.String(255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('settings')
