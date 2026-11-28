"""Novel write repository and combined read+write repository facade."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import and_, any_, func, or_, select, tuple_, update
from sqlalchemy import delete as _delete
from sqlalchemy.orm import Session

from copixiv.core.draft import NovelDraft
from copixiv.core.exceptions import NotFoundError
from copixiv.core.models import SOURCE_PIXIV, EpubStatus
from copixiv.core.time import parse_pixiv_time
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.features.sources.entity_resolver import EntityResolver
from copixiv.features.tags.resolver import TagAliasResolver
from copixiv.log import logger

from .read_repo import SQLAlchemyNovelReadRepository

# Passive metadata-only refreshes skip UPDATE when nothing meaningful changed
# (pure view-count bumps or <5 like jitter with identical core fields/tags),
# preserving PG HOT updates and avoiding GIN index / trigger write amplification.
_MIN_PASSIVE_LIKE_DELTA = 5
_NOISE_ONLY_FIELDS = frozenset({"view", "like"})
_VALID_NOVEL_FIELDS = frozenset(c.name for c in models.Novel.__table__.c)

# ``update_metadata_many`` allowed columns (author + series metadata edit).
BATCH_METADATA_COLUMNS = frozenset(
    {
        "author_id",
        "author_name",
        "series_id",
        "series_name",
        "series_index",
    }
)


class SQLAlchemyNovelWriteRepository:
    """Write repository for novel upserts, deletes, and tag/metadata mutations."""

    def __init__(self, session: Session):
        self.session = session

    def upsert_novels(self, novels: list[NovelDraft], force_update: list[str] | None = None) -> int:
        """Insert or update novels and sync their tags."""
        if not novels:
            return 0

        novel_dicts: list[dict[str, Any]] = [dict(n.__dict__) for n in novels]
        force_update = force_update or []

        self._ensure_parent_rows(novel_dicts)
        novel_tags_map = self._resolve_tag_aliases(novel_dicts)
        existing_map = self._fetch_existing_novels(novel_dicts)
        new_ids = self._upsert_rows(
            novel_dicts,
            existing_map,
            force_update,
            novel_tags_map,
        )
        return len(new_ids)

    def _ensure_parent_rows(self, novels: list[dict]) -> None:
        """Ensure author + series placeholder rows exist before novel writes."""
        author_ids = {n["author_id"] for n in novels if n.get("author_id")}
        series_ids = {n["series_id"] for n in novels if n.get("series_id")}
        EntityResolver(self.session).ensure_pixiv_parents(
            author_ids=author_ids,
            series_ids=series_ids,
        )

    # ---- upsert helpers -----------------------------------------------------

    def _resolve_tag_aliases(
        self,
        novels: list[dict],
    ) -> dict[int, set[str]]:
        resolver = TagAliasResolver(self.session)
        novel_tags_map: dict[int, set[str]] = {}
        for n in novels:
            raw_tags = n.pop("tags", None) or []
            mapped_tags = resolver.resolve_set(raw_tags)
            nid = n.get("id")
            if nid is not None:
                novel_tags_map[nid] = mapped_tags
        return novel_tags_map

    def _fetch_existing_novels(
        self,
        novels: list[dict],
    ) -> dict[int, Any]:
        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        if not all_ids:
            return {}
        stmt = select(models.Novel).where(models.Novel.id == any_(cast(Any, all_ids)))
        return {n.id: n for n in self.session.execute(stmt).scalars().all()}

    def _upsert_rows(
        self,
        novels: list[dict],
        existing_map: dict[int, Any],
        force_update: list[str],
        novel_tags_map: dict[int, set[str]] | None = None,
    ) -> list[int]:
        """Insert new or update existing novel rows with merged tags."""
        update_fields_set = {
            "like",
            "view",
            "title",
            "text",
            "caption",
            "series_name",
            "has_epub",
            *force_update,
        }

        new_ids: list[int] = []

        for novel in novels:
            filtered = {k: v for k, v in novel.items() if k in _VALID_NOVEL_FIELDS}
            if filtered.get("has_epub") is None:
                filtered.pop("has_epub", None)
            if "create_time" in filtered and isinstance(filtered["create_time"], str):
                filtered["create_time"] = parse_pixiv_time(filtered["create_time"])
            nid = int(novel["id"]) if novel.get("id") is not None else None
            existing = existing_map.get(nid) if nid is not None else None

            for int_field in ("id", "author_id", "series_id", "series_index"):
                if int_field in filtered and filtered[int_field] is not None:
                    filtered[int_field] = int(filtered[int_field])

            if existing:
                incoming_tags = (
                    novel_tags_map.get(nid, set()) if novel_tags_map and nid is not None else set()
                )
                current_tags = set(existing.tags or [])
                tags_changed = bool(incoming_tags - current_tags)

                pending_changes = {
                    key: value
                    for key, value in filtered.items()
                    if ((getattr(existing, key, None) is None and value) or key in update_fields_set)
                    and getattr(existing, key, None) != value
                }

                if not force_update and not tags_changed and set(pending_changes) <= _NOISE_ONLY_FIELDS:
                    like_delta = abs(int(pending_changes.get("like", existing.like)) - int(existing.like))
                    if like_delta < _MIN_PASSIVE_LIKE_DELTA:
                        continue

                for key, value in pending_changes.items():
                    setattr(existing, key, value)
                if tags_changed:
                    existing.tags = sorted(current_tags | incoming_tags)
            else:
                if novel_tags_map and nid is not None:
                    normalized_tags = sorted(novel_tags_map.get(nid, []))
                    filtered = {**filtered, "tags": normalized_tags}
                if not filtered.get("source"):
                    filtered["source"] = SOURCE_PIXIV
                if filtered.get("source_key") is None and nid is not None:
                    filtered["source_key"] = str(nid)
                new_novel = models.Novel(**filtered)
                self.session.add(new_novel)
                if nid is not None:
                    existing_map[nid] = new_novel
                    new_ids.append(nid)

        self.session.flush()

        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        logger.info(
            f"upsert_novels: {len(new_ids)} new (out of {len(novels)} total, {len(all_ids)} IDs queried)"
        )

        return new_ids

    def get_parent_ids(self, novel_ids: set[int]) -> tuple[set[int], set[int]]:
        """Return ``(author_ids, series_ids)`` currently stored for *novel_ids*."""
        if not novel_ids:
            return set(), set()
        rows = self.session.execute(
            select(models.Novel.author_id, models.Novel.series_id).where(
                models.Novel.id == any_(cast(Any, list(novel_ids)))
            )
        ).all()
        authors = {aid for aid, _ in rows if aid is not None}
        series = {sid for _, sid in rows if sid is not None}
        return authors, series

    def _cleanup_failed_novels(
        self,
        novel_ids: list[int],
        source_key_pairs: list[tuple[str, str]],
    ) -> None:
        if not novel_ids and not source_key_pairs:
            return
        if len(source_key_pairs) <= 5000:
            cond: Any = models.FailedNovel.novel_id == any_(cast(Any, novel_ids))
            if source_key_pairs:
                cond = or_(
                    cond,
                    tuple_(models.FailedNovel.source, models.FailedNovel.source_key).in_(source_key_pairs),
                )
            self.session.execute(_delete(models.FailedNovel).where(cond))
        else:
            self.session.execute(
                _delete(models.FailedNovel).where(models.FailedNovel.novel_id == any_(cast(Any, novel_ids)))
            )
            for i in range(0, len(source_key_pairs), 5000):
                pair_chunk = source_key_pairs[i : i + 5000]
                self.session.execute(
                    _delete(models.FailedNovel).where(
                        tuple_(models.FailedNovel.source, models.FailedNovel.source_key).in_(pair_chunk)
                    )
                )

    def delete(self, novel_id: int) -> tuple[bool, str | None]:
        """Delete a novel row and clean its ``failed_novel`` ledger entries.

        Returns ``(deleted, stored_path)``.
        """
        row = self.session.execute(
            _delete(models.Novel)
            .where(models.Novel.id == novel_id)
            .returning(
                models.Novel.path,
                models.Novel.source,
                models.Novel.source_key,
                models.Novel.series_id,
            )
        ).one_or_none()
        if row is None:
            return False, None
        cond: Any = models.FailedNovel.novel_id == novel_id
        if row.source and row.source_key:
            cond = or_(
                cond,
                and_(
                    models.FailedNovel.source == row.source,
                    models.FailedNovel.source_key == row.source_key,
                ),
            )
        self.session.execute(_delete(models.FailedNovel).where(cond))
        engine_delta.record_novel_deletes(self.session, [novel_id])
        if row.series_id is not None:
            engine_delta.record_series_dirty(self.session, [row.series_id])
        self.session.flush()
        return True, row.path

    def toggle_favourite(self, novel_id: int) -> None:
        result = self.session.execute(
            update(models.Novel)
            .where(models.Novel.id == novel_id)
            .values(is_favourite=~models.Novel.is_favourite)
            .returning(models.Novel.is_favourite)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Novel {novel_id} not found")
        engine_delta.record_novel_upserts(self.session, [novel_id])

    def toggle_series_favourite(self, series_id: int) -> bool:
        """Toggle favourite state for every chapter in *series_id*.

        If any chapter is currently favourited, sets all chapters to ``False``;
        otherwise sets all chapters to ``True``. Returns the new boolean state.
        """
        rows = self.session.execute(
            select(models.Novel.id, models.Novel.is_favourite).where(models.Novel.series_id == series_id)
        ).all()
        if not rows:
            raise NotFoundError(f"Series {series_id} not found")
        target = not any(bool(fav) for _, fav in rows)
        self.session.execute(
            update(models.Novel).where(models.Novel.series_id == series_id).values(is_favourite=target)
        )
        engine_delta.record_series_dirty(self.session, [series_id])
        self.session.flush()
        return target

    def delete_series(self, series_id: int) -> tuple[int, list[str]]:
        """Delete all chapters + series row for *series_id*; return ``(deleted_count, stored_paths)``."""
        rows = self.session.execute(
            _delete(models.Novel)
            .where(models.Novel.series_id == series_id)
            .returning(
                models.Novel.id,
                models.Novel.path,
                models.Novel.source,
                models.Novel.source_key,
            )
        ).all()
        deleted_ids = [int(row.id) for row in rows]
        source_key_pairs = [(row.source, row.source_key) for row in rows if row.source and row.source_key]
        self._cleanup_failed_novels(deleted_ids, source_key_pairs)
        self.session.execute(_delete(models.Series).where(models.Series.series_id == series_id))
        if deleted_ids:
            engine_delta.record_novel_deletes(self.session, deleted_ids)
        engine_delta.record_series_dirty(self.session, [series_id])
        self.session.flush()
        paths = [row.path for row in rows if row.path]
        return len(deleted_ids), paths

    def expand_series_ids(self, novel_ids: Sequence[int]) -> list[int]:
        """Expand *novel_ids* to include all sibling chapters of any represented series."""
        unique_ids = sorted({int(nid) for nid in novel_ids})
        if not unique_ids:
            return []
        series_ids = [
            int(sid)
            for sid in self.session.execute(
                select(models.Novel.series_id)
                .where(
                    models.Novel.id == any_(cast(Any, unique_ids)),
                    models.Novel.series_id.isnot(None),
                )
                .distinct()
            )
            .scalars()
            .all()
            if sid is not None
        ]
        if not series_ids:
            return unique_ids
        chapter_ids = [
            int(nid)
            for nid in self.session.execute(
                select(models.Novel.id).where(models.Novel.series_id.in_(series_ids))
            )
            .scalars()
            .all()
        ]
        return sorted(set(unique_ids) | set(chapter_ids))

    def update_has_epub_status(self, novel_ids: list[int], status: EpubStatus) -> None:
        if not novel_ids:
            return
        self.session.execute(
            update(models.Novel).where(models.Novel.id == any_(cast(Any, novel_ids))).values(has_epub=status)
        )
        engine_delta.record_novel_upserts(self.session, novel_ids)

    def update_metadata(self, novel_id: int, values: dict[str, Any]) -> None:
        """单行元数据更新 —— 卡片「编辑」的写路径（docs/NOVEL_EDIT_DESIGN.md §5）。"""
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        for field, value in values.items():
            setattr(novel, field, value)
        self.session.flush()

    # ---- batch operations ----------------------------------------------------

    def delete_many(self, novel_ids: list[int]) -> tuple[int, list[str]]:
        """Delete many novels and return ``(deleted_count, stored_paths)``."""
        if not novel_ids:
            return 0, []
        rows = self.session.execute(
            _delete(models.Novel)
            .where(models.Novel.id == any_(cast(Any, novel_ids)))
            .returning(
                models.Novel.id,
                models.Novel.path,
                models.Novel.source,
                models.Novel.source_key,
                models.Novel.series_id,
            )
        ).all()
        deleted_ids = [row.id for row in rows]
        deleted_series_ids = {int(row.series_id) for row in rows if row.series_id is not None}
        source_key_pairs = [(row.source, row.source_key) for row in rows if row.source and row.source_key]
        self._cleanup_failed_novels(deleted_ids, source_key_pairs)
        if deleted_ids:
            engine_delta.record_novel_deletes(self.session, deleted_ids)
        if deleted_series_ids:
            engine_delta.record_series_dirty(self.session, deleted_series_ids)
        self.session.flush()
        paths = [row.path for row in rows if row.path]
        return len(deleted_ids), paths

    def add_tags_to_novels(self, novel_ids: list[int], tags: set[str]) -> int:
        """Add *tags* to every listed novel."""
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        expr: Any = models.Novel.tags
        for t in tag_names:
            expr = func.array_append(func.array_remove(expr, t), t)
        changed = list(
            self.session.execute(
                update(models.Novel)
                .where(
                    models.Novel.id == any_(cast(Any, novel_ids)),
                    ~models.Novel.tags.contains(tag_names),
                )
                .values(tags=expr)
                .returning(models.Novel.id)
            )
            .scalars()
            .all()
        )
        if changed:
            engine_delta.record_novel_upserts(self.session, changed)
        return len(changed)

    def filter_ids_by_source(self, novel_ids: list[int], sources: set[str]) -> list[int]:
        """把 id 列表收窄到来源属于 *sources* 的那些（批量编辑只作用于本地书）。"""
        if not novel_ids or not sources:
            return []
        stmt = (
            select(models.Novel.id)
            .where(
                models.Novel.id == any_(cast(Any, novel_ids)),
                models.Novel.source.in_(sorted(sources)),
            )
            .order_by(models.Novel.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def update_metadata_many(
        self,
        novel_ids: list[int],
        values: dict[str, Any],
        *,
        only_where_null: str | None = None,
    ) -> int:
        """set-based 批量元数据写：一条 UPDATE 覆盖整个 id 列表，返回实际改动的行数。"""
        if not novel_ids or not values:
            return 0
        for field in values:
            if field not in BATCH_METADATA_COLUMNS:
                raise ValueError(f"不支持的批量元数据字段: {field}")
        if only_where_null is not None and (
            only_where_null not in BATCH_METADATA_COLUMNS or only_where_null not in values
        ):
            raise ValueError(f"only_where_null 必须是 values 里的批量元数据列: {only_where_null}")

        changed_conditions = [
            getattr(models.Novel, field).is_distinct_from(value) for field, value in values.items()
        ]
        conditions = [
            models.Novel.id == any_(cast(Any, novel_ids)),
            or_(*changed_conditions),
        ]
        if only_where_null is not None:
            conditions.append(getattr(models.Novel, only_where_null).is_(None))

        old_series_ids = {
            int(sid)
            for sid in self.session.execute(
                select(models.Novel.series_id)
                .where(*conditions, models.Novel.series_id.isnot(None))
                .distinct()
            )
            .scalars()
            .all()
            if sid is not None
        }

        changed = list(
            self.session.execute(
                update(models.Novel).where(*conditions).values(**values).returning(models.Novel.id)
            )
            .scalars()
            .all()
        )
        if changed:
            engine_delta.record_novel_upserts(self.session, changed)
            dirty_sids = set(old_series_ids)
            if isinstance(values.get("series_id"), int):
                dirty_sids.add(int(values["series_id"]))
            if dirty_sids:
                engine_delta.record_series_dirty(self.session, dirty_sids)
        return len(changed)

    def remove_tags_from_novels(self, novel_ids: list[int], tags: set[str]) -> int:
        """Remove *tags* from every listed novel."""
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        expr: Any = models.Novel.tags
        for t in tag_names:
            expr = func.array_remove(expr, t)
        changed = list(
            self.session.execute(
                update(models.Novel)
                .where(
                    models.Novel.id == any_(cast(Any, novel_ids)),
                    models.Novel.tags.overlap(tag_names),
                )
                .values(tags=expr)
                .returning(models.Novel.id)
            )
            .scalars()
            .all()
        )
        if changed:
            engine_delta.record_novel_upserts(self.session, changed)
        return len(changed)


class SQLAlchemyNovelRepository(
    SQLAlchemyNovelReadRepository,
    SQLAlchemyNovelWriteRepository,
):
    """Facade: read + write halves of the novel repository."""
