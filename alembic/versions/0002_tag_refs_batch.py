"""batch tag reference_count maintenance via statement-level trigger

Revision ID: 0002_tag_refs_batch
Revises: 0001_postgres_baseline
Create Date: 2026-09-03 20:10:00.000000

Replaces the per-row ``sync_tag_refs`` trigger with statement-level
triggers using transition tables (``OLD TABLE``/``NEW TABLE``).  Because
PostgreSQL forbids transition tables on multi-event triggers and on
triggers with column lists, there is one trigger per event
(INSERT / UPDATE / DELETE).  Consequences:

* Non-``tags`` updates (e.g. the daily ``update_author_name`` writeback)
  still fire the UPDATE trigger, but the trigger function early-exits when
  no row's ``tags`` array actually changed — so ``tag`` rows are never
  locked by author-name writebacks.
* Reference-count maintenance becomes set-based: one ``INSERT`` +
  one ``UPDATE ... FROM`` per triggering statement, with the delta
  aggregated over all rows in the transition table.  A statement that
  changes many novels locks only the tags that actually changed, and only
  once per statement instead of once per novel row.
"""

from typing import Union, Sequence

from alembic import op

revision: str = "0002_tag_refs_batch"
down_revision: Union[str, Sequence[str], None] = "0001_postgres_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_tag_refs_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- A statement-level UPDATE that does not actually change any tag array
  -- (e.g. author-name writeback) exits immediately — no tag rows touched.
  -- Kept inside a nested IF so INSERT/DELETE triggers (which only define
  -- NEW/OLD transition tables) never reference the UPDATE-only names.
  IF TG_OP = 'UPDATE' THEN
    IF NOT EXISTS (
      SELECT 1 FROM old_rows o JOIN new_rows n ON o.id = n.id
      WHERE o.tags IS DISTINCT FROM n.tags
    ) THEN
      RETURN NULL;
    END IF;
  END IF;

  -- Per-tag net delta for this *statement's* transition rows.  A tag that
  -- appears twice inside one novel's array contributes at most 1 (and -1)
  -- thanks to the DISTINCT on (row id, tag name); multiple distinct novel
  -- rows each contribute independently.
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

  -- Make sure every tag that gains at least one reference exists, then
  -- apply the whole delta set in one set-based UPDATE.
  INSERT INTO tag(name, reference_count)
    SELECT d.tag_name, 0 FROM _tag_ref_deltas d WHERE d.delta > 0
    ON CONFLICT (name) DO NOTHING;
  UPDATE tag SET reference_count = reference_count + d.delta
    FROM _tag_ref_deltas d
    WHERE tag.name = d.tag_name AND d.delta <> 0;

  RETURN NULL;
END; $$;
"""

_BATCH_TRIGGERS = """
DROP TRIGGER IF EXISTS trg_sync_tag_refs ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_insert ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_update ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_delete ON novel;
CREATE TRIGGER trg_sync_tag_refs_insert
  AFTER INSERT ON novel
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION sync_tag_refs_batch();
CREATE TRIGGER trg_sync_tag_refs_update
  AFTER UPDATE ON novel
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION sync_tag_refs_batch();
CREATE TRIGGER trg_sync_tag_refs_delete
  AFTER DELETE ON novel
  REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION sync_tag_refs_batch();
"""

_ROW_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_tag_refs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('DELETE','UPDATE') THEN
    UPDATE tag SET reference_count = reference_count - 1 WHERE name = ANY(OLD.tags);
  END IF;
  IF TG_OP IN ('INSERT','UPDATE') THEN
    INSERT INTO tag(name) SELECT DISTINCT unnest(NEW.tags) ON CONFLICT (name) DO NOTHING;
    UPDATE tag SET reference_count = reference_count + 1 WHERE name = ANY(NEW.tags);
  END IF;
  RETURN COALESCE(NEW, OLD);
END; $$;
"""

_ROW_TRIGGER = """
DROP TRIGGER IF EXISTS trg_sync_tag_refs ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_insert ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_update ON novel;
DROP TRIGGER IF EXISTS trg_sync_tag_refs_delete ON novel;
CREATE TRIGGER trg_sync_tag_refs AFTER INSERT OR UPDATE OR DELETE ON novel
  FOR EACH ROW EXECUTE FUNCTION sync_tag_refs();
"""


def upgrade() -> None:
    op.execute(_BATCH_FUNCTION)
    op.execute(_BATCH_TRIGGERS)
    # The old row-level function is no longer referenced by any trigger.
    op.execute("DROP FUNCTION IF EXISTS sync_tag_refs()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs ON novel")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_insert ON novel")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_update ON novel")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_tag_refs_delete ON novel")
    op.execute("DROP FUNCTION IF EXISTS sync_tag_refs_batch()")
    op.execute(_ROW_FUNCTION)
    op.execute(_ROW_TRIGGER)
