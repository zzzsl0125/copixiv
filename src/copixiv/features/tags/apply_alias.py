"""Tag alias application use cases (retroactive novel tag replacement)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from copixiv.db import delta as engine_delta
from copixiv.db import models

from .repo import SQLAlchemyTagRepository


def apply_alias_retroactively(session: Session, source: str, target: str) -> int:
    """Replace all occurrences of *source* tag with *target* tag across novels."""
    if source == target:
        return 0
    src_tag = session.execute(select(models.Tag).where(models.Tag.name == source)).scalar_one_or_none()
    if not src_tag:
        return 0
    tag_repo = SQLAlchemyTagRepository(session)
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
    return len(changed_ids)


def create_and_apply_alias(session: Session, source: str, target: str) -> dict:
    """Create a tag alias record and retroactively rewrite existing novels."""
    alias = SQLAlchemyTagRepository(session).create_alias({"source": source, "target": target})
    apply_alias_retroactively(session, source, target)
    return alias


def create_and_apply_aliases_batch(
    session: Session,
    sources: list[str],
    target: str,
) -> list[dict]:
    """Create multiple ``source -> target`` aliases and backfill novels in one transaction."""
    unique_sources = list(dict.fromkeys(sources))
    return [create_and_apply_alias(session, source, target) for source in unique_sources]


def replace_alias_group(
    session: Session,
    old_target: str,
    target: str,
    sources: list[str],
) -> list[dict]:
    """Atomically replace/rename an alias group and backfill novels on target rename."""
    tag_repo = SQLAlchemyTagRepository(session)
    unique_sources = list(dict.fromkeys(sources))

    if old_target == target:
        existing = tag_repo.get_aliases_by_target(target)
        existing_by_source = {row["source"]: row for row in existing}
        new_sources_set = set(unique_sources)
        to_remove_ids = [row["id"] for src, row in existing_by_source.items() if src not in new_sources_set]
        if to_remove_ids:
            tag_repo.delete_aliases_by_ids(to_remove_ids)
        result: list[dict] = []
        for source in unique_sources:
            if source in existing_by_source:
                result.append({"id": existing_by_source[source]["id"], "source": source, "target": target})
            else:
                result.append(create_and_apply_alias(session, source, target))
        return result

    existing_old = tag_repo.get_aliases_by_target(old_target)
    if existing_old:
        tag_repo.delete_aliases_by_ids(row["id"] for row in existing_old)

    existing_target = tag_repo.get_aliases_by_target(target)
    existing_target_by_source = {row["source"]: row for row in existing_target}

    result = []
    for source in unique_sources:
        if source in existing_target_by_source:
            result.append({"id": existing_target_by_source[source]["id"], "source": source, "target": target})
        else:
            result.append(create_and_apply_alias(session, source, target))

    apply_alias_retroactively(session, old_target, target)
    tag_repo.prune_unused_tags([old_target])
    return result
