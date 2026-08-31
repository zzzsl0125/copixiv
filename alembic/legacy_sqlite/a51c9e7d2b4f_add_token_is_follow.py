"""add_token_is_follow

Revision ID: a51c9e7d2b4f
Revises: f1a2b3c4d5e6
Create Date: 2026-08-25 09:00:00.000000

Add ``is_follow`` to the ``tokens`` table — the designated「追更账号」
(used by the novel_follow / user_follow_add / user_follow_delete flows).

This replaces the static ``config.pixiv_accounts.follow`` string as the
single source of truth for which account owns the Pixiv following-list
feed.  Multiple accounts may be flagged simultaneously only if app logic
is buggy; the account pool picks the flagged one via ``force_follow``.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'a51c9e7d2b4f'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pragmatic try/pass like the existing SQLite migrations: the column
    # may already exist (model-driven creation on a fresh install).
    try:
        op.execute(
            "ALTER TABLE tokens ADD COLUMN is_follow BOOLEAN DEFAULT 0 NOT NULL"
        )
    except Exception:
        pass


def downgrade() -> None:
    # SQLite has no DROP COLUMN before 3.35 with batch mode; pragmatic
    # no-op, matching the existing migrations' approach.
    pass
