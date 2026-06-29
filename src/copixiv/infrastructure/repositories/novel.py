"""Novel repository — full implementation."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    select, func, case, and_, update, delete as _delete, text,
)
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from .base import BaseRepository
from .fts import FTSManager
from .tag import TagRepository
from .query_builder import NovelQueryBuilder


class NovelRepository(BaseRepository):
    """Repository for novel CRUD and queries."""

    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.UPDATABLE_NOVEL_FIELDS = list(
            self.VALID_NOVEL_FIELDS - {C.COL_ID, C.COL_INDEX}
        )
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    # ---- read ----------------------------------------------------------------

    async def get_by_id(self, novel_id: int) -> dict | None:
        novel = self.session.get(models.Novel, novel_id)
        return self._row_to_dict(novel) if novel else None

    async def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    async def get_novels(
        self,
        queries: dict[str, str] | None = None,
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
        cursor: dict | None = None,
        per_page: int = 50,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> dict:
        # Validate fields
        if order_by:
            self._validate_query_field(order_by)
        if queries:
            for q_type in queries.values():
                self._validate_query_field(q_type)

        # Random pool shortcut
        if order_by == "random" and not queries:
            novels = self._get_random_novels(
                per_page, min_like or 0, min_text or 0
            )
            return {
                "cursor": {"random_page": True},
                "novels": self._process_novel_rows(novels),
            }

        params = {
            "queries": queries,
            "order_by": order_by,
            "order_direction": order_direction,
            "cursor": cursor,
            "per_page": per_page + 1,
            "min_like": min_like,
            "min_text": min_text,
        }

        builder = NovelQueryBuilder(self, **params)
        query, query_params = builder.build()

        result = self.session.execute(query, query_params)
        novels = [dict(row._mapping) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > per_page:
            n = novels.pop()
            cursor_out = {"id": n["id"], order_by: n.get(order_by)}

        novels = self._process_novel_rows(novels)
        return {"novels": novels, "cursor": cursor_out}

    async def count_novels(
        self,
        queries: dict[str, str] | None = None,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> int:
        if queries:
            for q_type in queries.values():
                self._validate_query_field(q_type)

        params = {
            "queries": queries or {},
            "min_like": min_like,
            "min_text": min_text,
        }
        builder = NovelQueryBuilder(self, **params)
        id_subq = builder._build_id_filter_subquery(count_mode=True)
        count_stmt = select(func.count()).select_from(id_subq)
        result = self.session.execute(count_stmt, builder.params)
        return result.scalar() or 0

    # ---- write ---------------------------------------------------------------

    async def upsert_novels(
        self, novels: list[dict], force_update: list[str] | None = None
    ) -> int:
        if not novels:
            return 0

        force_update = force_update or []
        tag_repo = TagRepository(self.session)
        alias_map = tag_repo.get_alias_map_sync()

        novel_tags_map: dict[int, set[str]] = {}
        processed: list[dict] = []

        for n in novels:
            mapped_tags = {alias_map.get(t, t) for t in n.pop("tag", [])}
            nid = n.get("id")
            if nid is not None:
                novel_tags_map[nid] = mapped_tags
            processed.append(n)

        update_fields_set = set([
            "like", "view", "title", "text", "caption",
            "series_id", "series_name", "series_index", "create_time",
        ] + force_update)

        # Batch-fetch existing
        all_ids = [int(n["id"]) for n in processed if n.get("id")]
        existing_map: dict[int, Any] = {}
        if all_ids:
            stmt = select(models.Novel).where(models.Novel.id.in_(all_ids))
            existing_map = {
                n.id: n
                for n in self.session.execute(stmt).scalars().all()
            }

        new_ids: list[int] = []
        fts_dirty_ids: list[int] = []

        for novel in processed:
            filtered = {
                k: v for k, v in novel.items()
                if k in self.VALID_NOVEL_FIELDS
            }
            nid = int(novel["id"]) if novel.get("id") else None
            existing = existing_map.get(nid) if nid else None

            for int_field in ("id", "author_id", "series_id", "series_index"):
                if int_field in filtered and filtered[int_field] is not None:
                    filtered[int_field] = int(filtered[int_field])

            if existing:
                for key, value in filtered.items():
                    if (getattr(existing, key, None) is None and value) or key in update_fields_set:
                        setattr(existing, key, value)
                fts_fields = (C.COL_TITLE, C.COL_AUTHOR_NAME, C.COL_SERIES_NAME)
                if nid and any(
                    key in filtered
                    and str(getattr(existing, key, None)) != str(filtered[key])
                    for key in fts_fields
                ):
                    fts_dirty_ids.append(nid)
            else:
                new_novel = models.Novel(**filtered)
                self.session.add(new_novel)
                new_ids.append(novel.get("id"))

        self.session.flush()

        # Tags
        for nid, tag_list in novel_tags_map.items():
            self.rewrite_tags(nid, set(tag_list))

        # FTS
        fts = FTSManager(self.session)
        fts.update_novel_fts_index(list(set(new_ids + fts_dirty_ids)))

        return len(new_ids)

    async def update_field(self, novel_id: int, field: str, value: Any) -> None:
        if field not in self.UPDATABLE_NOVEL_FIELDS:
            raise ValueError(f"Invalid or non-updatable field: {field}")
        novel = self.session.get(models.Novel, novel_id)
        if novel is not None:
            setattr(novel, field, value)

    async def delete(self, novel_id: int) -> None:
        FTSManager(self.session).delete_novel_fts(novel_id)
        novel = self.session.get(models.Novel, novel_id)
        if novel is not None:
            self.session.delete(novel)

    async def toggle_favourite(self, novel_id: int) -> None:
        fav = self.session.execute(
            select(models.Favourite).where(
                models.Favourite.novel_id == novel_id
            )
        ).scalar_one_or_none()
        if fav:
            self.session.delete(fav)
        else:
            self.session.add(models.Favourite(novel_id=novel_id))

    async def toggle_special_follow(self, author_id: int) -> None:
        follow = self.session.execute(
            select(models.SpecialFollow).where(
                models.SpecialFollow.author_id == author_id
            )
        ).scalar_one_or_none()
        if follow:
            self.session.delete(follow)
        else:
            self.session.add(models.SpecialFollow(author_id=author_id))

    async def update_has_epub_status(
        self, novel_ids: list[int], status: int
    ) -> None:
        if not novel_ids:
            return
        self.session.execute(
            update(models.Novel)
            .where(models.Novel.id.in_(novel_ids))
            .values(has_epub=status)
        )

    # ---- tags ----------------------------------------------------------------

    def rewrite_tags(self, novel_id: int, new_tags: set[str]) -> None:
        if not new_tags:
            self.session.execute(
                delete(models.NovelTag).where(
                    models.NovelTag.novel_id == novel_id
                )
            )
            return

        existing = set(self.session.execute(
            select(models.Tag.name)
            .join(models.NovelTag, models.Tag.id == models.NovelTag.tag_id)
            .where(models.NovelTag.novel_id == novel_id)
        ).scalars().all())

        to_add = new_tags - existing
        to_remove = existing - new_tags

        if to_remove:
            tag_ids_stmt = select(models.Tag.id).where(
                models.Tag.name.in_(to_remove)
            )
            self.session.execute(
                delete(models.NovelTag).where(
                    models.NovelTag.novel_id == novel_id,
                    models.NovelTag.tag_id.in_(tag_ids_stmt),
                )
            )
            self._update_tag_ref_count(to_remove, -1)

        if to_add:
            self._add_tags(novel_id, to_add)

    def _add_tags(self, novel_id: int, tags: set[str]) -> None:
        self.session.execute(
            sqlite_insert(models.Tag)
            .values([{"name": t} for t in tags])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        tag_ids = self.session.execute(
            select(models.Tag.id).where(models.Tag.name.in_(tags))
        ).scalars().all()
        if tag_ids:
            self.session.bulk_insert_mappings(
                models.NovelTag,
                [{"novel_id": novel_id, "tag_id": tid} for tid in tag_ids],
            )
        self._update_tag_ref_count(tags, 1)

    def _update_tag_ref_count(self, tags: set[str], delta: int) -> None:
        if not tags:
            return
        self.session.execute(
            update(models.Tag)
            .where(models.Tag.name.in_(tags))
            .values(reference_count=models.Tag.reference_count + delta)
        )

    # ---- random pool ---------------------------------------------------------

    def _get_random_novels(
        self, limit: int, min_likes: int, min_texts: int
    ) -> list[dict]:
        stmt = (
            select(models.RandomNovelPool.novel_id)
            .where(
                models.RandomNovelPool.min_likes == min_likes,
                models.RandomNovelPool.min_texts == min_texts,
            )
            .order_by(func.random())
            .limit(limit)
        )
        ids = [row[0] for row in self.session.execute(stmt).all()]
        if not ids:
            return []
        novels = self.session.execute(
            select(models.Novel).where(models.Novel.id.in_(ids))
        ).scalars().all()
        novel_dicts = [self._row_to_dict(n) for n in novels]

        # Load tags so _process_novel_rows can split them (same shape as the
        # GROUP_CONCAT label produced by _build_main_query).
        tag_query = (
            select(
                models.NovelTag.novel_id,
                func.group_concat(models.Tag.name, "|||").label(C.COL_TAGS),
            )
            .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.NovelTag.novel_id.in_(ids))
            .group_by(models.NovelTag.novel_id)
        )
        tag_map = {
            row[0]: row[1]
            for row in self.session.execute(tag_query).all()
        }
        for nd in novel_dicts:
            nd[C.COL_TAGS] = tag_map.get(nd[C.COL_ID])

        return novel_dicts

    async def populate_random_novel_pool(
        self, min_likes: int, min_texts: int
    ) -> None:
        existing_ids = set(
            row[0] for row in self.session.execute(
                select(models.RandomNovelPool.novel_id).where(
                    models.RandomNovelPool.min_likes == min_likes,
                    models.RandomNovelPool.min_texts == min_texts,
                )
            ).all()
        )
        stmt = select(models.Novel.id).where(
            models.Novel.like >= min_likes,
            models.Novel.text >= min_texts,
        )
        if existing_ids:
            stmt = stmt.where(models.Novel.id.notin_(existing_ids))
        new_ids = [row[0] for row in self.session.execute(stmt).all()]
        if new_ids:
            self.session.execute(
                sqlite_insert(models.RandomNovelPool),
                [
                    {"novel_id": nid, "min_likes": min_likes, "min_texts": min_texts}
                    for nid in new_ids
                ],
            )

    # ---- helpers -------------------------------------------------------------

    def _validate_query_field(self, field: str) -> None:
        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValueError(f"Invalid query field: {field}")

    def _row_to_dict(self, obj: Any) -> dict:
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

    def _process_novel_rows(self, novels: list[dict]) -> list[dict]:
        for novel in novels:
            tags_str = novel.get(C.COL_TAGS)
            if tags_str and isinstance(tags_str, str):
                if "|||" in tags_str:
                    novel[C.COL_TAGS] = tags_str.split("|||")
                elif "," in tags_str:
                    novel[C.COL_TAGS] = tags_str.split(",")
                else:
                    novel[C.COL_TAGS] = [tags_str]
            else:
                novel[C.COL_TAGS] = []
        return novels
