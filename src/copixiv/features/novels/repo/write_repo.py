"""Novel write repository and combined read+write repository facade."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import any_, func, or_, select, update
from sqlalchemy import delete as _delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.core.draft import NovelDraft
from copixiv.core.exceptions import NotFoundError
from copixiv.core.models import EpubStatus
from copixiv.core.services import parse_pixiv_time
from copixiv.db import constants as C
from copixiv.db import models
from copixiv.db.base import BaseRepository, refresh_parent_summaries
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.log import logger

from .read_repo import SQLAlchemyNovelReadRepository
from .series_repo import SQLAlchemySeriesRepository

# ``update_metadata_many`` allowed columns (author + series metadata edit).
BATCH_METADATA_COLUMNS = frozenset({
    "author_id", "author_name", "series_id", "series_name", "series_index",
})


def _summary_ids(novel) -> tuple[set[int], set[int]]:
    """The ``(author_ids, series_ids)`` a novel row contributes to."""
    return (
        {novel.author_id} if novel.author_id else set(),
        {novel.series_id} if novel.series_id else set(),
    )


class SQLAlchemyNovelWriteRepository(BaseRepository):
    """Write repository for novel upserts, deletes, and tag/metadata mutations."""

    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    def upsert_novels(
        self, novels: list[NovelDraft], force_update: list[str] | None = None
    ) -> int:
        """Insert or update novels and sync their tags."""
        if not novels:
            return 0

        novel_dicts: list[dict[str, Any]] = [
            dict(n) if isinstance(n, dict) else dict(n.__dict__)
            for n in novels
        ]
        force_update = force_update or []

        self._ensure_parent_rows(novel_dicts)
        novel_tags_map = self._resolve_tag_aliases(novel_dicts)
        existing_map = self._fetch_existing_novels(novel_dicts)
        new_ids = self._upsert_rows(
            novel_dicts, existing_map, force_update, novel_tags_map,
        )
        return len(new_ids)

    def _ensure_parent_rows(self, novels: list[dict]) -> None:
        """Ensure author + series placeholder rows exist before novel writes."""
        from copixiv.features.authors.repo import SQLAlchemyAuthorRepository

        author_ids = {n["author_id"] for n in novels if n.get("author_id")}
        series_ids = {n["series_id"] for n in novels if n.get("series_id")}
        if author_ids:
            SQLAlchemyAuthorRepository(self.session).ensure_exists(author_ids)
        if series_ids:
            SQLAlchemySeriesRepository(self.session).ensure_exists(series_ids)

    # ---- upsert helpers -----------------------------------------------------

    def _resolve_tag_aliases(
        self, novels: list[dict],
    ) -> dict[int, set[str]]:
        tag_repo = SQLAlchemyTagRepository(self.session)
        alias_map = tag_repo.get_alias_map()
        novel_tags_map: dict[int, set[str]] = {}
        for n in novels:
            raw_tags = n.pop("tags", None)
            if raw_tags is None:
                raw_tags = n.pop("tag", [])
            mapped_tags = {alias_map.get(t, t) for t in raw_tags}
            nid = n.get("id")
            if nid is not None:
                novel_tags_map[nid] = mapped_tags
        return novel_tags_map

    def _fetch_existing_novels(
        self, novels: list[dict],
    ) -> dict[int, Any]:
        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        if not all_ids:
            return {}
        stmt = select(models.Novel).where(models.Novel.id.in_(all_ids))
        return {
            n.id: n
            for n in self.session.execute(stmt).scalars().all()
        }

    def _upsert_rows(
        self,
        novels: list[dict],
        existing_map: dict[int, Any],
        force_update: list[str],
        novel_tags_map: dict[int, set[str]] | None = None,
    ) -> list[int]:
        """Insert new or update existing novel rows with merged tags."""
        update_fields_set = set([
            "like", "view", "title", "text", "caption",
            "series_name", "has_epub",
        ] + force_update)

        new_ids: list[int] = []

        for novel in novels:
            filtered = {
                k: v for k, v in novel.items()
                if k in self.VALID_NOVEL_FIELDS
            }
            if filtered.get(C.COL_HAS_EPUB) is None:
                filtered.pop(C.COL_HAS_EPUB, None)
            if C.COL_CREATE_TIME in filtered and isinstance(
                filtered[C.COL_CREATE_TIME], str
            ):
                filtered[C.COL_CREATE_TIME] = parse_pixiv_time(
                    filtered[C.COL_CREATE_TIME]
                )
            nid = int(novel["id"]) if novel.get("id") is not None else None
            existing = existing_map.get(nid) if nid is not None else None

            for int_field in ("id", "author_id", "series_id", "series_index"):
                if int_field in filtered and filtered[int_field] is not None:
                    filtered[int_field] = int(filtered[int_field])

            if existing:
                for key, value in filtered.items():
                    if (getattr(existing, key, None) is None and value) or key in update_fields_set:
                        setattr(existing, key, value)
                if novel_tags_map and nid is not None:
                    incoming = novel_tags_map.get(nid, set())
                    current = set(existing.tags or [])
                    if incoming - current:
                        existing.tags = sorted(current | incoming)
            else:
                if novel_tags_map and nid is not None:
                    normalized_tags = sorted(novel_tags_map.get(nid, []))
                    filtered = {**filtered, "tags": normalized_tags}
                new_novel = models.Novel(**filtered)
                if "shuffle" not in filtered or not filtered["shuffle"]:
                    import random as _random
                    new_novel.shuffle = _random.randint(0, 2**31 - 1)
                self.session.add(new_novel)
                if nid is not None:
                    existing_map[nid] = new_novel
                    new_ids.append(nid)

        self.session.flush()

        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        logger.info(
            f"upsert_novels: {len(new_ids)} new "
            f"(out of {len(novels)} total, {len(all_ids)} IDs queried)"
        )

        return new_ids

    def get_parent_ids(self, novel_ids: set[int]) -> tuple[set[int], set[int]]:
        """Return ``(author_ids, series_ids)`` currently stored for *novel_ids*."""
        if not novel_ids:
            return set(), set()
        rows = self.session.execute(
            select(models.Novel.author_id, models.Novel.series_id).where(
                models.Novel.id.in_(novel_ids)
            )
        ).all()
        authors = {aid for aid, _ in rows if aid is not None}
        series = {sid for _, sid in rows if sid is not None}
        return authors, series

    def _refresh_summaries(self, parent_ids: tuple[set[int], set[int]]) -> None:
        """Recompute the author / series aggregates a novel write affected."""
        author_ids, series_ids = parent_ids
        refresh_parent_summaries(
            self.session,
            author_ids=author_ids,
            series_ids=series_ids,
        )

    def delete(self, novel_id: int) -> None:
        """Delete a novel row and clean its ``failed_novel`` ledger entries."""
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        parent_ids = _summary_ids(novel)
        self.session.execute(
            _delete(models.FailedNovel).where(
                models.FailedNovel.novel_id == novel_id
            )
        )
        self.session.delete(novel)
        self.session.flush()
        self._refresh_summaries(parent_ids)

    def toggle_favourite(self, novel_id: int) -> None:
        result = self.session.execute(
            update(models.Novel)
            .where(models.Novel.id == novel_id)
            .values(is_favourite=~models.Novel.is_favourite)
            .returning(models.Novel.is_favourite)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Novel {novel_id} not found")

    def toggle_special_follow(self, author_id: int) -> None:
        result = self.session.execute(
            update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(is_special_follow=~models.Author.is_special_follow)
            .returning(models.Author.is_special_follow)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Author {author_id} not found")

    def update_has_epub_status(
        self, novel_ids: list[int], status: EpubStatus
    ) -> None:
        if not novel_ids:
            return
        self.session.execute(
            update(models.Novel)
            .where(models.Novel.id.in_(novel_ids))
            .values(has_epub=status)
        )

    def update_metadata(self, novel_id: int, values: dict[str, Any]) -> None:
        """单行元数据更新 —— 卡片「编辑」的写路径（docs/NOVEL_EDIT_DESIGN.md §5）。"""
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        for field, value in values.items():
            setattr(novel, field, value)
        self.session.flush()

    # ---- batch operations ----------------------------------------------------

    def delete_many(self, novel_ids: list[int]) -> list[str]:
        """Delete many novels and return stored paths for disk cleanup."""
        if not novel_ids:
            return []
        rows = self.session.execute(
            select(models.Novel.path, models.Novel.author_id, models.Novel.series_id)
            .where(models.Novel.id.in_(novel_ids))
        ).all()
        self.session.execute(
            _delete(models.FailedNovel).where(
                models.FailedNovel.novel_id.in_(novel_ids)
            )
        )
        self.session.execute(
            _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
        )
        self.session.flush()
        self._refresh_summaries(
            ({row.author_id for row in rows}, {row.series_id for row in rows})
        )
        return [row.path for row in rows if row.path]

    def add_tags_to_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Add *tags* to every listed novel."""
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        self.session.execute(
            pg_insert(models.Tag)
            .values([{"name": t, "reference_count": 0} for t in tag_names])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        expr: Any = models.Novel.tags
        for t in tag_names:
            expr = func.array_append(func.array_remove(expr, t), t)
        changed = list(self.session.execute(
            update(models.Novel)
            .where(
                models.Novel.id.in_(novel_ids),
                ~models.Novel.tags.contains(tag_names),
            )
            .values(tags=expr)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)

    def filter_ids_by_source(
        self, novel_ids: list[int], sources: set[str]
    ) -> list[int]:
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
            only_where_null not in BATCH_METADATA_COLUMNS
            or only_where_null not in values
        ):
            raise ValueError(
                f"only_where_null 必须是 values 里的批量元数据列: {only_where_null}"
            )

        changed_conditions = [
            getattr(models.Novel, field).is_distinct_from(value)
            for field, value in values.items()
        ]
        conditions = [
            models.Novel.id == any_(cast(Any, novel_ids)),
            or_(*changed_conditions),
        ]
        if only_where_null is not None:
            conditions.append(
                getattr(models.Novel, only_where_null).is_(None)
            )

        changed = list(self.session.execute(
            update(models.Novel)
            .where(*conditions)
            .values(**values)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)

    def remove_tags_from_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Remove *tags* from every listed novel."""
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        expr: Any = models.Novel.tags
        for t in tag_names:
            expr = func.array_remove(expr, t)
        changed = list(self.session.execute(
            update(models.Novel)
            .where(
                models.Novel.id.in_(novel_ids),
                models.Novel.tags.overlap(tag_names),
            )
            .values(tags=expr)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)


class SQLAlchemyNovelRepository(
    SQLAlchemyNovelReadRepository,
    SQLAlchemyNovelWriteRepository,
):
    """Facade: read + write halves of the novel repository."""
