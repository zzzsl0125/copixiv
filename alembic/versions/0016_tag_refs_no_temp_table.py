"""tag reference_count maintenance: eliminate per-statement temp table churn

Revision ID: 0016_tag_refs_no_temp_table
Revises: 0015_repair_jsonb_strings
Create Date: 2026-11-14 00:00:00.000000

Replaces ``CREATE TEMP TABLE IF NOT EXISTS _tag_ref_deltas ... ON COMMIT DROP``
in :func:`sync_tag_refs_batch` with direct set-based SQL over the transition
tables (``new_rows`` / ``old_rows``).

Why remove the temp table: ``ON COMMIT DROP`` allocates and drops entries in
PostgreSQL's system catalogs (``pg_class``, ``pg_attribute``, ``pg_type``,
``pg_depend``) on *every* statement-level ``INSERT`` / ``UPDATE`` / ``DELETE``
on ``novel`` — including non-tag updates such as ``toggle_favourite`` or
``update_has_epub_status`` — generating steady catalog dead-tuple bloat.

Computing positive deltas with ``INSERT INTO tag ... ON CONFLICT DO UPDATE``
(ordered by ``tag_name`` for deterministic lock ordering) and negative deltas
with ``UPDATE tag ... FROM (...)`` preserves the linear ``O(rows * tags)``
complexity of migration 0004 while eliminating all catalog writes.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0016_tag_refs_no_temp_table"
down_revision: Union[str, Sequence[str], None] = "0015_repair_jsonb_strings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NO_TEMP_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_tag_refs_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO tag(name, reference_count)
      SELECT d.tag_name, d.cnt
      FROM (
        SELECT tag_name, COUNT(*)::INTEGER AS cnt
        FROM (
          SELECT DISTINCT r.id, t.tag_name
          FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
        ) x
        GROUP BY tag_name
      ) d
      ORDER BY d.tag_name
      ON CONFLICT (name) DO UPDATE
        SET reference_count = tag.reference_count + EXCLUDED.reference_count;

  ELSIF TG_OP = 'DELETE' THEN
    UPDATE tag
      SET reference_count = tag.reference_count - d.cnt
      FROM (
        SELECT tag_name, COUNT(*)::INTEGER AS cnt
        FROM (
          SELECT DISTINCT r.id, t.tag_name
          FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
        ) x
        GROUP BY tag_name
      ) d
      WHERE tag.name = d.tag_name;

  ELSE -- UPDATE: linear net delta without creating/dropping temp tables
    INSERT INTO tag(name, reference_count)
      SELECT pos.tag_name, pos.delta
      FROM (
        SELECT tag_name, SUM(delta)::INTEGER AS delta
        FROM (
          SELECT DISTINCT r.id, t.tag_name, 1::BIGINT AS delta
            FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
          UNION ALL
          SELECT DISTINCT r.id, t.tag_name, -1::BIGINT AS delta
            FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
        ) raw
        GROUP BY tag_name
        HAVING SUM(delta) > 0
      ) pos
      ORDER BY pos.tag_name
      ON CONFLICT (name) DO UPDATE
        SET reference_count = tag.reference_count + EXCLUDED.reference_count;

    UPDATE tag
      SET reference_count = tag.reference_count + neg.delta
      FROM (
        SELECT tag_name, SUM(delta)::INTEGER AS delta
        FROM (
          SELECT DISTINCT r.id, t.tag_name, 1::BIGINT AS delta
            FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
          UNION ALL
          SELECT DISTINCT r.id, t.tag_name, -1::BIGINT AS delta
            FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
        ) raw
        GROUP BY tag_name
        HAVING SUM(delta) < 0
      ) neg
      WHERE tag.name = neg.tag_name;
  END IF;

  RETURN NULL;
END; $$;
"""


_PREVIOUS_LINEAR_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_tag_refs_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS _tag_ref_deltas (tag_name TEXT, delta BIGINT) ON COMMIT DROP;
  DELETE FROM _tag_ref_deltas;

  IF TG_OP = 'INSERT' THEN
    INSERT INTO _tag_ref_deltas(tag_name, delta)
      SELECT tag_name, COUNT(*)::BIGINT FROM (
        SELECT DISTINCT r.id, t.tag_name
          FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
      ) d GROUP BY tag_name;
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO _tag_ref_deltas(tag_name, delta)
      SELECT tag_name, -COUNT(*)::BIGINT FROM (
        SELECT DISTINCT r.id, t.tag_name
          FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
      ) d GROUP BY tag_name;
  ELSE -- UPDATE: net delta; unchanged tags cancel out and are dropped
    INSERT INTO _tag_ref_deltas(tag_name, delta)
      SELECT tag_name, SUM(delta)::BIGINT FROM (
        SELECT DISTINCT r.id, t.tag_name, 1::BIGINT AS delta
          FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
        UNION ALL
        SELECT DISTINCT r.id, t.tag_name, -1::BIGINT AS delta
          FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
      ) d GROUP BY tag_name HAVING SUM(delta) <> 0;
  END IF;

  INSERT INTO tag(name, reference_count)
    SELECT d.tag_name, 0 FROM _tag_ref_deltas d WHERE d.delta > 0
    ON CONFLICT (name) DO NOTHING;
  UPDATE tag SET reference_count = reference_count + d.delta
    FROM _tag_ref_deltas d
    WHERE tag.name = d.tag_name AND d.delta <> 0;

  RETURN NULL;
END; $$;
"""


def upgrade() -> None:
    op.execute(_NO_TEMP_FUNCTION)


def downgrade() -> None:
    op.execute(_PREVIOUS_LINEAR_FUNCTION)
