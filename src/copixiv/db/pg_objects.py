"""PostgreSQL-only custom DDL objects (sequences, trigger functions, triggers, table tuning).

Single source of truth imported by Alembic migrations (starting at
``0001_baseline``) and by the baseline transition script so ``alembic check``
and PostgreSQL runtime behavior stay identical.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

LOCAL_COORD_SEQUENCE_SQL = """
CREATE SEQUENCE IF NOT EXISTS novel_local_coord_seq
    AS bigint
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO CYCLE
"""

TAG_REFS_SYNC_FUNCTION_SQL = """
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

TAG_REFS_TRIGGER_STATEMENTS = (
    "DROP TRIGGER IF EXISTS trg_sync_tag_refs ON novel",
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


TABLE_ANALYZE_TUNING_STATEMENTS = (
    "ALTER TABLE novel SET (autovacuum_analyze_scale_factor = 0.02, autovacuum_analyze_threshold = 500)",
    "ALTER TABLE tag SET (autovacuum_analyze_scale_factor = 0.05, autovacuum_analyze_threshold = 500)",
)


def install_pg_objects(conn: Connection) -> None:
    """Install all custom sequences, trigger functions, triggers, and table tuning idempotently."""
    conn.execute(text(LOCAL_COORD_SEQUENCE_SQL))
    conn.execute(text(TAG_REFS_SYNC_FUNCTION_SQL))
    for stmt in TAG_REFS_TRIGGER_STATEMENTS:
        conn.execute(text(stmt))
    for stmt in TABLE_ANALYZE_TUNING_STATEMENTS:
        conn.execute(text(stmt))


def uninstall_pg_objects(conn: Connection) -> None:
    """Drop custom triggers, trigger functions, and sequences cleanly."""
    if conn.execute(text("SELECT to_regclass('novel')")).scalar() is not None:
        for trg in (
            "trg_sync_tag_refs",
            "trg_sync_tag_refs_insert",
            "trg_sync_tag_refs_update",
            "trg_sync_tag_refs_delete",
        ):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trg} ON novel"))
    conn.execute(text("DROP FUNCTION IF EXISTS sync_tag_refs_batch()"))
    conn.execute(text("DROP SEQUENCE IF EXISTS novel_local_coord_seq"))


__all__ = [
    "LOCAL_COORD_SEQUENCE_SQL",
    "TABLE_ANALYZE_TUNING_STATEMENTS",
    "TAG_REFS_SYNC_FUNCTION_SQL",
    "TAG_REFS_TRIGGER_STATEMENTS",
    "install_pg_objects",
    "uninstall_pg_objects",
]
