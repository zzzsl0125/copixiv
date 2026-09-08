"""tag reference_count maintenance: drop the O(rows²) UPDATE early-exit

Revision ID: 0004_tag_refs_linear
Revises: 0003_search_expression_index
Create Date: 2026-09-10 00:00:00.000000

Migration 0002 made ``sync_tag_refs`` statement-level, and guarded the
``UPDATE`` branch with an early exit so that a statement which does not touch
``novel.tags`` (author-name writeback, ``has_epub`` sync, any metadata
refresh) would leave the ``tag`` table alone::

    IF NOT EXISTS (
      SELECT 1 FROM old_rows o JOIN new_rows n ON o.id = n.id
      WHERE o.tags IS DISTINCT FROM n.tags
    ) THEN RETURN NULL; END IF;

The intent is right, but the check joins two **transition tables**, which have
no indexes and no statistics — so the planner falls back to a nested loop and
the check costs O(rows²).  Measured on PostgreSQL 16 (10,000-row UPDATE that
does not touch tags): **26.2 s** with the trigger, **1.0 s** with the trigger
disabled; the same predicate over the real ``novel`` table (primary key +
statistics) plans as a merge join in milliseconds.

This migration removes the special case instead of tuning it: the net delta is
already computed by unnesting both transition tables, and a row whose tags did
not change contributes ``+1`` and ``-1`` for the same ``(id, tag_name)``, which
cancels to zero.  Adding ``HAVING SUM(delta) <> 0`` drops exactly those rows,
so the tag table is still untouched — the work is now linear in
``rows × tags`` instead of quadratic, and the branch no longer depends on the
planner's choice of join strategy.

Correctness is unchanged: for each tag the net is
``(#rows gaining it) − (#rows losing it)``, so a tag that one novel loses while
another gains it nets to zero and correctly leaves ``reference_count`` alone.

Downgrade restores the exact function body from migration 0002.
"""

from typing import Union, Sequence

from alembic import op

revision: str = "0004_tag_refs_linear"
down_revision: Union[str, Sequence[str], None] = "0003_search_expression_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LINEAR_FUNCTION = """
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

# The exact body created by migration 0002 (restored on downgrade).
_QUADRATIC_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_tag_refs_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NOT EXISTS (
      SELECT 1 FROM old_rows o JOIN new_rows n ON o.id = n.id
      WHERE o.tags IS DISTINCT FROM n.tags
    ) THEN
      RETURN NULL;
    END IF;
  END IF;

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
  ELSE -- UPDATE
    INSERT INTO _tag_ref_deltas(tag_name, delta)
      SELECT tag_name, SUM(delta)::BIGINT FROM (
        SELECT DISTINCT r.id, t.tag_name, 1::BIGINT AS delta
          FROM new_rows AS r, unnest(r.tags) AS t(tag_name)
        UNION ALL
        SELECT DISTINCT r.id, t.tag_name, -1::BIGINT AS delta
          FROM old_rows AS r, unnest(r.tags) AS t(tag_name)
      ) d GROUP BY tag_name;
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
    # The three statement-level triggers from migration 0002 keep pointing at
    # this function; CREATE OR REPLACE preserves their bindings.
    op.execute(_LINEAR_FUNCTION)


def downgrade() -> None:
    op.execute(_QUADRATIC_FUNCTION)
