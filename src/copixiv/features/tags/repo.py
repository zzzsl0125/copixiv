"""Consolidated tag, preference, alias, and suggestion repository."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from sqlalchemy import delete as _delete
from sqlalchemy import exists, func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, aliased

from copixiv.core.exceptions import ValidationError
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import delete_by_id, reorder_rows
from copixiv.query_engine.adapter import RustNovelQueryEngine


class SQLAlchemyTagRepository:
    """Repository for tag entities, preferences, aliases, and suggestions."""

    def __init__(
        self,
        session: Session,
        *,
        query_engine: RustNovelQueryEngine | None = None,
    ):
        self.session = session
        self._query_engine: RustNovelQueryEngine | None = (
            query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
        )

    # ------------------------------------------------------------------
    # Tag entity & orphan pruning
    # ------------------------------------------------------------------

    def get_or_create_tag_id(self, name: str) -> int:
        """Return ``tag.id`` for *name*, inserting a row if missing."""
        tag = self.session.execute(select(models.Tag).where(models.Tag.name == name)).scalar_one_or_none()
        if tag:
            return tag.id
        self.session.execute(
            pg_insert(models.Tag)
            .values(name=name, reference_count=0)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        self.session.flush()
        engine_delta.record_tags_dirty(self.session, [name])
        tag = self.session.execute(select(models.Tag).where(models.Tag.name == name)).scalar_one()
        return tag.id

    @staticmethod
    def _unaliased_zero_predicate():
        return (
            models.Tag.reference_count <= 0,
            ~exists().where(
                or_(
                    models.TagAlias.source == models.Tag.id,
                    models.TagAlias.target == models.Tag.id,
                )
            ),
        )

    def prune_unused_tags(self, tag_names: Iterable[str] | None = None) -> int:
        """Delete zero-reference tag rows not referenced by any ``tag_alias``."""
        stmt = _delete(models.Tag).where(*self._unaliased_zero_predicate())
        if tag_names is not None:
            names = sorted({n for n in tag_names if n})
            if not names:
                return 0
            stmt = stmt.where(models.Tag.name.in_(names))
        deleted = list(self.session.execute(stmt.returning(models.Tag.name)).scalars().all())
        if deleted:
            engine_delta.record_tags_dirty(self.session, deleted)
        return len(deleted)

    def prune_unused_tag_ids(self, tag_ids: Iterable[int]) -> int:
        """Delete zero-reference tag rows by ID when no ``tag_alias`` references them."""
        ids = sorted(set(tag_ids))
        if not ids:
            return 0
        stmt = (
            _delete(models.Tag)
            .where(*self._unaliased_zero_predicate(), models.Tag.id.in_(ids))
            .returning(models.Tag.name)
        )
        deleted = list(self.session.execute(stmt).scalars().all())
        if deleted:
            engine_delta.record_tags_dirty(self.session, deleted)
        return len(deleted)

    # ------------------------------------------------------------------
    # Tag preferences (favourite / blocked)
    # ------------------------------------------------------------------

    def get_preferences(self) -> Sequence[Any]:
        stmt = select(models.TagPreference).order_by(
            models.TagPreference.sort_index,
            models.TagPreference.id,
        )
        return list(self.session.execute(stmt).scalars().all())

    def create_preference(self, pref_data: dict) -> Any:
        data = dict(pref_data)
        tag_name = str(data["tag"])
        existing_id = self.session.execute(
            select(models.TagPreference.id)
            .where(func.lower(models.TagPreference.tag) == tag_name.lower())
            .limit(1)
        ).scalar_one_or_none()
        if existing_id is not None:
            raise ValidationError("该标签偏好已存在")

        if data.get("sort_index") is None:
            data["sort_index"] = int(
                self.session.execute(
                    select(func.coalesce(func.max(models.TagPreference.sort_index), -1) + 1)
                ).scalar_one()
            )
        pref = models.TagPreference(**data)
        self.session.add(pref)
        self.session.flush()
        return pref

    def delete_preference(self, pref_id: int) -> bool:
        return delete_by_id(self.session, models.TagPreference, pref_id)

    def reorder_preferences(self, ids: list[int]) -> int:
        return reorder_rows(self.session, models.TagPreference, "sort_index", ids)

    # ------------------------------------------------------------------
    # Tag aliases
    # ------------------------------------------------------------------

    def get_aliases(self) -> Sequence[dict]:
        """Return all tag aliases with source/target as tag names."""
        src_tag = aliased(models.Tag)
        tgt_tag = aliased(models.Tag)
        rows = self.session.execute(
            select(
                models.TagAlias.id,
                src_tag.name,
                tgt_tag.name,
            )
            .select_from(models.TagAlias)
            .join(src_tag, models.TagAlias.source == src_tag.id)
            .join(tgt_tag, models.TagAlias.target == tgt_tag.id)
            .order_by(models.TagAlias.id)
        ).all()
        return [{"id": row[0], "source": row[1], "target": row[2]} for row in rows]

    def get_alias_map(self) -> dict[str, str]:
        """Return ``{source_tag_name: target_tag_name}`` for all aliases."""
        src_tag = aliased(models.Tag)
        tgt_tag = aliased(models.Tag)
        rows = self.session.execute(
            select(src_tag.name, tgt_tag.name)
            .select_from(models.TagAlias)
            .join(src_tag, models.TagAlias.source == src_tag.id)
            .join(tgt_tag, models.TagAlias.target == tgt_tag.id)
        ).all()
        return {src: tgt for src, tgt in rows}

    def get_aliases_by_target(self, target_name: str) -> list[dict]:
        """Return all alias rows targeting *target_name*, including tag IDs."""
        src_tag = aliased(models.Tag)
        tgt_tag = aliased(models.Tag)
        rows = self.session.execute(
            select(
                models.TagAlias.id,
                models.TagAlias.source,
                models.TagAlias.target,
                src_tag.name,
                tgt_tag.name,
            )
            .select_from(models.TagAlias)
            .join(src_tag, models.TagAlias.source == src_tag.id)
            .join(tgt_tag, models.TagAlias.target == tgt_tag.id)
            .where(tgt_tag.name == target_name)
            .order_by(models.TagAlias.id)
        ).all()
        return [
            {
                "id": row[0],
                "source_id": row[1],
                "target_id": row[2],
                "source": row[3],
                "target": row[4],
            }
            for row in rows
        ]

    def create_alias(self, alias_data: dict) -> dict:
        """Create a tag alias enforcing single-hop and acyclic invariants."""
        source_name = alias_data["source"]
        target_name = alias_data["target"]
        if source_name == target_name:
            raise ValidationError("原标签不能和目标标签相同")

        source_id = self.get_or_create_tag_id(source_name)
        target_id = self.get_or_create_tag_id(target_name)

        existing_reverse = self.session.execute(
            select(models.TagAlias.target).where(models.TagAlias.source == target_id)
        ).scalar_one_or_none()
        if existing_reverse is not None:
            if existing_reverse == source_id:
                raise ValidationError(f"不能在 '{source_name}' 和 '{target_name}' 之间创建循环别名")
            raise ValidationError(f"目标标签 '{target_name}' 已作为别名指向其他标签（不支持多级别名）")

        incoming_source = self.session.execute(
            select(models.TagAlias.id).where(models.TagAlias.target == source_id).limit(1)
        ).scalar_one_or_none()
        if incoming_source is not None:
            raise ValidationError(f"原标签 '{source_name}' 已被其他别名作为目标标签（不支持多级别名）")

        alias = models.TagAlias(source=source_id, target=target_id)
        self.session.add(alias)
        self.session.flush()

        return {
            "id": alias.id,
            "source": source_name,
            "target": target_name,
        }

    def delete_aliases_by_ids(self, alias_ids: Iterable[int]) -> int:
        """Delete multiple aliases by ID and prune any resulting orphan tags."""
        ids = sorted(set(alias_ids))
        if not ids:
            return 0
        rows = self.session.execute(
            select(models.TagAlias.source, models.TagAlias.target).where(models.TagAlias.id.in_(ids))
        ).all()
        if not rows:
            return 0
        tag_ids = {tid for pair in rows for tid in pair}
        deleted = cast(
            CursorResult[Any],
            self.session.execute(_delete(models.TagAlias).where(models.TagAlias.id.in_(ids))),
        ).rowcount
        self.session.flush()
        engine_delta.record_aliases_dirty(self.session)
        self.prune_unused_tag_ids(tag_ids)
        return int(deleted or 0)

    def delete_aliases_by_target(self, target_name: str) -> int:
        """Delete all aliases pointing to *target_name* and prune orphan tags."""
        existing = self.get_aliases_by_target(target_name)
        if not existing:
            return 0
        return self.delete_aliases_by_ids(row["id"] for row in existing)

    # ------------------------------------------------------------------
    # Suggestions & retroactive application
    # ------------------------------------------------------------------

    def suggest_aliases(
        self,
        limit: int = 5,
        offset: int = 0,
        target_tag: str | None = None,
    ) -> dict:
        """Suggest alias mappings using the in-process Rust ``TagIndex``."""
        return suggest_tag_aliases(
            self.session,
            limit=limit,
            offset=offset,
            target_tag=target_tag,
            query_engine=self._query_engine,
        )

    def apply_alias_retroactively(self, source: str, target: str) -> int:
        """Replace all occurrences of *source* tag with *target* tag on novels."""
        return apply_alias_retroactively(self.session, source, target)


# ---------------------------------------------------------------------------
# Tag alias suggestions (Rust TagIndex)
# ---------------------------------------------------------------------------


def _require_engine(
    session: Session,
    query_engine: RustNovelQueryEngine | None = None,
) -> RustNovelQueryEngine:
    eng = query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
    if eng is None or not eng.ready():
        raise RuntimeError("Rust query_engine is not attached or not ready on this session")
    engine_delta.sync_engine_if_needed(session, eng)
    return eng


def suggest_tag_aliases(
    session: Session,
    *,
    limit: int = 5,
    offset: int = 0,
    target_tag: str | None = None,
    query_engine: RustNovelQueryEngine | None = None,
) -> dict:
    """Suggest tag alias groupings using the in-process Rust engine."""
    eng = _require_engine(session, query_engine)
    items_rs, next_cursor = eng.suggest_tag_aliases(
        limit=limit,
        offset=int(offset or 0),
        target_tag=target_tag,
    )
    items = [
        {
            "target": {
                "id": item.target.id,
                "name": item.target.name,
                "reference_count": item.target.reference_count,
            },
            "candidates": [
                {
                    "id": cand.id,
                    "name": cand.name,
                    "reference_count": cand.reference_count,
                }
                for cand in item.candidates
            ],
        }
        for item in items_rs
    ]
    return {
        "items": items,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Retroactive alias application & group management
# ---------------------------------------------------------------------------


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
