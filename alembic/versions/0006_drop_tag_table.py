"""Drop PG tag table/triggers and convert tag_alias columns to TEXT.

Revision ID: 0006_drop_tag_table
Revises: 0005_align_schema_drift
Create Date: 2026-11-23 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_drop_tag_table"
down_revision: str | Sequence[str] | None = "0005_align_schema_drift"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TAG_REFS_SYNC_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION sync_tag_refs_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO tag(name, reference_count)
      SELECT DISTINCT t.tag_name, 0
      FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
      WHERE NOT EXISTS (SELECT 1 FROM tag AS e WHERE e.name = t.tag_name)
      ORDER BY t.tag_name
      ON CONFLICT (name) DO NOTHING;

    WITH deltas AS MATERIALIZED (
      SELECT tag_name, COUNT(*)::INTEGER AS delta
      FROM (
        SELECT DISTINCT r.id, t.tag_name
        FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
      ) x
      GROUP BY tag_name
    ),
    locked AS (
      SELECT t.id, d.delta
      FROM tag AS t
      JOIN deltas AS d ON d.tag_name = t.name
      ORDER BY t.id
      FOR NO KEY UPDATE OF t
    )
    UPDATE tag
      SET reference_count = tag.reference_count + locked.delta
      FROM locked
      WHERE tag.id = locked.id;

  ELSIF TG_OP = 'DELETE' THEN
    WITH deltas AS MATERIALIZED (
      SELECT tag_name, COUNT(*)::INTEGER AS cnt
      FROM (
        SELECT DISTINCT r.id, t.tag_name
        FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
      ) x
      GROUP BY tag_name
    ),
    locked AS (
      SELECT t.id, d.cnt
      FROM tag AS t
      JOIN deltas AS d ON d.tag_name = t.name
      ORDER BY t.id
      FOR NO KEY UPDATE OF t
    )
    UPDATE tag
      SET reference_count = GREATEST(0, tag.reference_count - locked.cnt)
      FROM locked
      WHERE tag.id = locked.id;

  ELSE -- UPDATE
    IF NOT EXISTS (
      SELECT id, tags FROM new_rows
      EXCEPT
      SELECT id, tags FROM old_rows
    ) THEN
      RETURN NULL;
    END IF;

    INSERT INTO tag(name, reference_count)
      SELECT DISTINCT t.tag_name, 0
      FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
      WHERE NOT EXISTS (SELECT 1 FROM tag AS e WHERE e.name = t.tag_name)
      ORDER BY t.tag_name
      ON CONFLICT (name) DO NOTHING;

    WITH deltas AS MATERIALIZED (
      SELECT tag_name, SUM(delta)::INTEGER AS delta
      FROM (
        SELECT DISTINCT r.id, t.tag_name, 1::BIGINT AS delta
          FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
        UNION ALL
        SELECT DISTINCT r.id, t.tag_name, -1::BIGINT AS delta
          FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
      ) raw
      GROUP BY tag_name
      HAVING SUM(delta) <> 0
    ),
    locked AS (
      SELECT t.id, d.delta
      FROM tag AS t
      JOIN deltas AS d ON d.tag_name = t.name
      ORDER BY t.id
      FOR NO KEY UPDATE OF t
    )
    UPDATE tag
      SET reference_count = GREATEST(0, tag.reference_count + locked.delta)
      FROM locked
      WHERE tag.id = locked.id;
  END IF;

  RETURN NULL;
END; $$;
"""

_TAG_REFS_TRIGGER_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_sync_tag_refs_insert ON novel",
    "DROP TRIGGER IF EXISTS trg_sync_tag_refs_update ON novel",
    "DROP TRIGGER IF EXISTS trg_sync_tag_refs_delete ON novel",
    """
    CREATE TRIGGER trg_sync_tag_refs_insert
      AFTER INSERT ON novel
      REFERENCING NEW TABLE AS new_rows
      FOR EACH STATEMENT
      EXECUTE FUNCTION sync_tag_refs_batch()
    """,
    """
    CREATE TRIGGER trg_sync_tag_refs_update
      AFTER UPDATE ON novel
      REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
      FOR EACH STATEMENT
      EXECUTE FUNCTION sync_tag_refs_batch()
    """,
    """
    CREATE TRIGGER trg_sync_tag_refs_delete
      AFTER DELETE ON novel
      REFERENCING OLD TABLE AS old_rows
      FOR EACH STATEMENT
      EXECUTE FUNCTION sync_tag_refs_batch()
    """,
)


def upgrade() -> None:
    op.execute("ALTER TABLE tag_alias ADD COLUMN source_text TEXT")
    op.execute("ALTER TABLE tag_alias ADD COLUMN target_text TEXT")
    op.execute(
        """
        UPDATE tag_alias AS ta
        SET source_text = s.name,
            target_text = t.name
        FROM tag AS s, tag AS t
        WHERE ta.source = s.id
          AND ta.target = t.id
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_tag_alias_target")
    op.execute(
        """
        ALTER TABLE tag_alias
            DROP CONSTRAINT IF EXISTS tag_alias_source_key,
            DROP CONSTRAINT IF EXISTS tag_alias_source_fkey,
            DROP CONSTRAINT IF EXISTS tag_alias_target_fkey
        """
    )
    op.execute("ALTER TABLE tag_alias DROP COLUMN source, DROP COLUMN target")
    op.execute("ALTER TABLE tag_alias RENAME COLUMN source_text TO source")
    op.execute("ALTER TABLE tag_alias RENAME COLUMN target_text TO target")
    op.execute(
        """
        ALTER TABLE tag_alias
            ALTER COLUMN source SET NOT NULL,
            ALTER COLUMN target SET NOT NULL,
            ADD CONSTRAINT tag_alias_source_key UNIQUE (source),
            ADD CONSTRAINT ck_tag_alias_distinct CHECK (source <> target)
        """
    )
    op.execute("CREATE INDEX ix_tag_alias_target ON tag_alias (target)")

    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_insert ON novel")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_update ON novel")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_delete ON novel")
    op.execute("DROP FUNCTION IF EXISTS sync_tag_refs_batch()")
    op.execute("DROP TABLE IF EXISTS tag")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE tag (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            reference_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "ALTER TABLE tag SET (autovacuum_analyze_scale_factor = 0.05, autovacuum_analyze_threshold = 500)"
    )

    op.execute(
        """
        INSERT INTO tag (name, reference_count)
        SELECT tag_name, COUNT(*)::INTEGER AS reference_count
        FROM (
            SELECT DISTINCT n.id, t.tag_name
            FROM novel AS n, unnest(n.tags) AS t(tag_name)
        ) x
        GROUP BY tag_name
        ORDER BY tag_name
        """
    )
    op.execute(
        """
        INSERT INTO tag (name, reference_count)
        SELECT DISTINCT name, 0
        FROM (
            SELECT source AS name FROM tag_alias
            UNION
            SELECT target AS name FROM tag_alias
        ) aliased
        WHERE NOT EXISTS (SELECT 1 FROM tag AS e WHERE e.name = aliased.name)
        ORDER BY name
        ON CONFLICT (name) DO NOTHING
        """
    )

    op.execute("ALTER TABLE tag_alias ADD COLUMN source_id BIGINT")
    op.execute("ALTER TABLE tag_alias ADD COLUMN target_id BIGINT")
    op.execute(
        """
        UPDATE tag_alias AS ta
        SET source_id = s.id,
            target_id = t.id
        FROM tag AS s, tag AS t
        WHERE ta.source = s.name
          AND ta.target = t.name
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_tag_alias_target")
    op.execute(
        """
        ALTER TABLE tag_alias
            DROP CONSTRAINT IF EXISTS tag_alias_source_key,
            DROP CONSTRAINT IF EXISTS ck_tag_alias_distinct
        """
    )
    op.execute("ALTER TABLE tag_alias DROP COLUMN source, DROP COLUMN target")
    op.execute("ALTER TABLE tag_alias RENAME COLUMN source_id TO source")
    op.execute("ALTER TABLE tag_alias RENAME COLUMN target_id TO target")
    op.execute(
        """
        ALTER TABLE tag_alias
            ALTER COLUMN source SET NOT NULL,
            ALTER COLUMN target SET NOT NULL,
            ADD CONSTRAINT tag_alias_source_key UNIQUE (source),
            ADD CONSTRAINT tag_alias_source_fkey FOREIGN KEY (source) REFERENCES tag(id),
            ADD CONSTRAINT tag_alias_target_fkey FOREIGN KEY (target) REFERENCES tag(id)
        """
    )
    op.execute("CREATE INDEX ix_tag_alias_target ON tag_alias (target)")

    op.execute(_TAG_REFS_SYNC_FUNCTION_SQL)
    for stmt in _TAG_REFS_TRIGGER_STATEMENTS:
        op.execute(stmt)
