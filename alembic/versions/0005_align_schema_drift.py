"""Align legacy schema drift on series_dead_letter.series_id and tag_preference constraint.

Revision ID: 0005_align_schema_drift
Revises: 0004_novel_shuffle_and_indexes
Create Date: 2026-11-22 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_align_schema_drift"
down_revision: str | Sequence[str] | None = "0004_novel_shuffle_and_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE series_dead_letter ALTER COLUMN series_id DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS series_dead_letter_series_id_seq")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = current_schema()
                  AND t.relname = 'tag_preference'
                  AND c.conname = 'tag_preference_preference_check'
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = current_schema()
                  AND t.relname = 'tag_preference'
                  AND c.conname = 'ck_tag_preference_value'
            ) THEN
                ALTER TABLE tag_preference
                    RENAME CONSTRAINT tag_preference_preference_check TO ck_tag_preference_value;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    pass
