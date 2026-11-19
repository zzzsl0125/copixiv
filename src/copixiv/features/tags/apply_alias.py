"""Tag alias application use cases (retroactive novel tag replacement)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from copixiv.db import delta as engine_delta
from copixiv.db import models

from .alias_repo import TagAliasRepository
from .tag_repo import TagRepository


def apply_alias_retroactively(session: Session, source: str, target: str) -> int:
    """Replace all occurrences of *source* tag with *target* tag across novels."""
    src_tag = session.execute(select(models.Tag).where(models.Tag.name == source)).scalar_one_or_none()
    if not src_tag:
        return 0
    tag_repo = TagRepository(session)
    tag_repo.get_or_create_tag_id(target)

    changed_ids = list(
        session.execute(
            sa_text(
                """
                UPDATE novel
                SET tags = ARRAY(
                    SELECT DISTINCT x
                    FROM unnest(array_replace(tags, :source, :target)) AS x
                )
                WHERE tags @> ARRAY[:source]::text[]
                RETURNING id
                """
            ),
            {"source": source, "target": target},
        )
        .scalars()
        .all()
    )
    if changed_ids:
        engine_delta.record_novel_upserts(session, changed_ids)
        engine_delta.record_tags_dirty(session, [source, target])
    tag_repo.prune_unused_tags([source])
    return len(changed_ids)


def create_and_apply_alias(session: Session, source: str, target: str) -> dict:
    """Create a tag alias record and retroactively rewrite existing novels."""
    alias = TagAliasRepository(session).create_alias({"source": source, "target": target})
    apply_alias_retroactively(session, source, target)
    return alias
