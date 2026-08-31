"""normalize_tag_aliases_fk

Revision ID: af8d332686b1
Revises: 4bdb5c432fa2
Create Date: 2026-06-29 22:35:34.002247

Convert tag_aliases.source and tag_aliases.target from strings to integer
foreign keys pointing to tag.id.  Uses raw SQL for reliability with SQLite.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = 'af8d332686b1'
down_revision: Union[str, Sequence[str], None] = '4bdb5c432fa2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert tag_aliases.source/target from strings to integer FKs.

    Steps (raw SQL, no batch mode):
      1. Drop existing indexes on source/target.
      2. Rename old string columns -> source_str, target_str.
      3. Add new INTEGER columns source, target (nullable).
      4. Resolve tag names to IDs and update.
      5. Drop source_str, target_str.
      6. Make source, target NOT NULL; add FK constraints and indexes.
    """
    conn = op.get_bind()

    # Check if already migrated
    info = conn.execute(sa.text("PRAGMA table_info('tag_aliases')")).fetchall()
    col_types = {row[1]: (row[2] or "").upper() for row in info}
    if "INT" in col_types.get("source", ""):
        return  # Already INTEGER — skip

    # 1. Drop old indexes
    op.execute("DROP INDEX IF EXISTS ix_tag_aliases_source")
    op.execute("DROP INDEX IF EXISTS ix_tag_aliases_target")

    # 2. Rename old string columns
    op.execute("ALTER TABLE tag_aliases RENAME COLUMN source TO source_str")
    op.execute("ALTER TABLE tag_aliases RENAME COLUMN target TO target_str")

    # 3. Add new INTEGER columns
    op.execute("ALTER TABLE tag_aliases ADD COLUMN source INTEGER")
    op.execute("ALTER TABLE tag_aliases ADD COLUMN target INTEGER")

    # 4. Resolve tag names → IDs
    tag_rows = conn.execute(sa.text("SELECT id, name FROM tag")).fetchall()
    name_to_id = {row[1]: row[0] for row in tag_rows}

    alias_rows = conn.execute(
        sa.text("SELECT id, source_str, target_str FROM tag_aliases")
    ).fetchall()

    updated = 0
    for alias_id, src_str, tgt_str in alias_rows:
        src_id = name_to_id.get(src_str)
        tgt_id = name_to_id.get(tgt_str)
        if src_id is not None and tgt_id is not None:
            conn.execute(
                sa.text(
                    "UPDATE tag_aliases SET source = :src, target = :tgt "
                    "WHERE id = :id"
                ),
                {"src": src_id, "tgt": tgt_id, "id": alias_id},
            )
            updated += 1
        else:
            # Tag name doesn't exist — delete the orphan alias
            conn.execute(
                sa.text("DELETE FROM tag_aliases WHERE id = :id"),
                {"id": alias_id},
            )

    # 5. Drop old string columns (SQLite 3.35+ supports DROP COLUMN)
    op.execute("ALTER TABLE tag_aliases DROP COLUMN source_str")
    op.execute("ALTER TABLE tag_aliases DROP COLUMN target_str")

    # 6. Recreate table with NOT NULL + FK (SQLite can't alter constraints)
    #    Build new table, copy data, swap
    op.execute("""
        CREATE TABLE _tag_aliases_new (
            id INTEGER NOT NULL,
            source INTEGER NOT NULL REFERENCES tag(id),
            target INTEGER NOT NULL REFERENCES tag(id),
            PRIMARY KEY (id)
        )
    """)
    op.execute("""
        INSERT INTO _tag_aliases_new (id, source, target)
        SELECT id, source, target FROM tag_aliases
    """)
    op.execute("DROP TABLE tag_aliases")
    op.execute("ALTER TABLE _tag_aliases_new RENAME TO tag_aliases")

    # Create indexes
    op.execute(
        "CREATE UNIQUE INDEX ix_tag_aliases_source ON tag_aliases (source)"
    )
    op.execute(
        "CREATE INDEX ix_tag_aliases_target ON tag_aliases (target)"
    )


def downgrade() -> None:
    """Revert tag_aliases back to string columns."""
    conn = op.get_bind()

    info = conn.execute(sa.text("PRAGMA table_info('tag_aliases')")).fetchall()
    col_types = {row[1]: (row[2] or "").upper() for row in info}
    if "VARCHAR" in col_types.get("source", ""):
        return  # Already VARCHAR — skip

    # Drop FK indexes
    op.execute("DROP INDEX IF EXISTS ix_tag_aliases_source")
    op.execute("DROP INDEX IF EXISTS ix_tag_aliases_target")

    # Create string-based table
    op.execute("""
        CREATE TABLE _tag_aliases_old (
            id INTEGER NOT NULL,
            source VARCHAR NOT NULL,
            target VARCHAR NOT NULL,
            PRIMARY KEY (id)
        )
    """)

    # Map IDs back to names
    tag_rows = conn.execute(sa.text("SELECT id, name FROM tag")).fetchall()
    id_to_name = {row[0]: row[1] for row in tag_rows}

    alias_rows = conn.execute(
        sa.text("SELECT id, source, target FROM tag_aliases")
    ).fetchall()

    for alias_id, src_id, tgt_id in alias_rows:
        src_name = id_to_name.get(src_id, f"<unknown:{src_id}>")
        tgt_name = id_to_name.get(tgt_id, f"<unknown:{tgt_id}>")
        conn.execute(
            sa.text(
                "INSERT INTO _tag_aliases_old (id, source, target) "
                "VALUES (:id, :src, :tgt)"
            ),
            {"id": alias_id, "src": src_name, "tgt": tgt_name},
        )

    op.execute("DROP TABLE tag_aliases")
    op.execute("ALTER TABLE _tag_aliases_old RENAME TO tag_aliases")

    op.execute(
        "CREATE UNIQUE INDEX ix_tag_aliases_source ON tag_aliases (source)"
    )
    op.execute(
        "CREATE INDEX ix_tag_aliases_target ON tag_aliases (target)"
    )
