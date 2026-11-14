"""Novel read repository (queries, listing, scopes, blocked-tag exclusion)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import Novel
from copixiv.core.services import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    QuerySpec,
    resolve_active,
)
from copixiv.db import constants as C
from copixiv.db import models
from copixiv.db.base import BaseRepository

from .query_builder import (
    NovelQueryBuilder,
    blocked_tags_excluded,
    time_axis_col,
)

# Bounded count cap — matches ``batch_operations.BATCH_MAX_NOVELS``.
COUNT_CAP = 5000


@dataclass(frozen=True)
class NovelCount:
    """有界计数结果：``value`` 加上「它是不是下界」。"""

    value: int
    truncated: bool = False


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM ``Novel`` to the domain :class:`Novel` model."""
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return Novel(**d)


def _novel_from_mapping(mapping) -> Novel:
    """Convert a row ``RowMapping`` (column-based select) to a domain Novel."""
    d = dict(mapping)
    if C.FIELD_IS_SPECIAL_FOLLOW in d:
        d[C.FIELD_IS_SPECIAL_FOLLOW] = bool(d[C.FIELD_IS_SPECIAL_FOLLOW])
    return Novel(**d)


def _attach_special_follow(session: Session, novels: list[Novel]) -> None:
    """Populate ``is_special_follow`` on *novels* from ``author`` in one query."""
    author_ids = {n.author_id for n in novels if n.author_id}
    if not author_ids:
        for novel in novels:
            novel.is_special_follow = False
        return
    sf = set(
        session.execute(
            select(models.Author.author_id).where(
                models.Author.is_special_follow == True,
                models.Author.author_id.in_(author_ids),
            )
        )
        .scalars()
        .all()
    )
    for novel in novels:
        novel.is_special_follow = novel.author_id in sf


class SQLAlchemyNovelReadRepository(BaseRepository):
    """Read-only repository for novel queries and scope filtering."""

    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    def get_by_id(self, novel_id: int) -> Novel | None:
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return None
        domain = _novel_from_orm(novel)
        _attach_special_follow(self.session, [domain])
        return domain

    def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    def get_stored_paths(self, novel_ids: set[int]) -> dict[int, str]:
        """``{id: path}`` for the given ids — the on-disk anchor of each row."""
        if not novel_ids:
            return {}
        stmt = select(models.Novel.id, models.Novel.path).where(
            models.Novel.id.in_(novel_ids),
            models.Novel.path.isnot(None),
        )
        return {
            nid: path
            for nid, path in self.session.execute(stmt).all()
            if path
        }

    # ---- blocked-tag exclusion helpers -------------------------------------

    def _exclusion_active(self, explicit: bool | None) -> bool:
        row = self.session.execute(
            select(models.Setting).where(
                models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY
            )
        ).scalar_one_or_none()
        return resolve_active(
            explicit, row.value if row is not None else None,
        )

    def _blocked_tag_names(self) -> frozenset[str]:
        rows = self.session.execute(
            select(models.TagPreference.tag).where(
                models.TagPreference.preference
                == models.TagPreferenceORM.blocked
            )
        ).scalars().all()
        return frozenset(rows)

    def _blocked_novel_ids(self, names: frozenset[str]) -> list[int]:
        """All novel IDs carrying any blocked tag (``tags && ...``)."""
        if not names:
            return []
        return list(self.session.execute(
            select(models.Novel.id).where(
                models.Novel.tags.overlap(list(names))
            )
        ).scalars().all())

    def _validate_and_resolve_blocked(self, spec: QuerySpec) -> frozenset[str]:
        """Validate filter condition fields on *spec* and return active blocked tags."""
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)
        return (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )

    def list_blocked_ids(self) -> list[int]:
        if not self._exclusion_active(None):
            return []
        return self._blocked_novel_ids(self._blocked_tag_names())

    def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / view / text / create_time)."""
        if not novel_ids or order_by not in (
            C.COL_ID,
            C.COL_LIKES,
            C.COL_VIEWS,
            C.COL_TEXTS,
            C.COL_CREATE_TIME,
        ):
            return list(novel_ids)

        col = (
            time_axis_col(models.Novel)
            if order_by == C.COL_ID
            else getattr(models.Novel, order_by)
        )
        descending = order_direction.upper() == "DESC"
        stmt = (
            select(models.Novel.id)
            .where(models.Novel.id.in_(novel_ids))
            .order_by(
                col.desc() if descending else col.asc(),
                models.Novel.id.desc() if descending else models.Novel.id.asc(),
            )
        )
        return list(self.session.execute(stmt).scalars())

    def get_novels(self, spec: QuerySpec) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
        if spec.order_by:
            self._validate_query_field(spec.order_by)
        self._validate_order_direction(spec.order_direction)
        blocked_names = self._validate_and_resolve_blocked(spec)

        # Random browsing — precomputed shuffle column for fast index seek.
        if spec.order_by == "random" and not spec.conditions:
            if not spec.cursor:
                novels = self._get_random_novels_shuffle(
                    spec.per_page, spec.min_like or 0, spec.min_text or 0,
                    blocked_names,
                )
                cursor_out = None
                if novels and len(novels) >= spec.per_page:
                    last = novels[-1]
                    cursor_out = {"shuffle": last.shuffle, "id": last.id}
                return {"cursor": cursor_out, "novels": novels}

        page_spec = spec.model_copy(update={"per_page": spec.per_page + 1})

        builder = NovelQueryBuilder(
            self, page_spec, blocked_tag_names=blocked_names,
        )
        query, _ = builder.build()

        result = self.session.execute(query)
        novels = [_novel_from_mapping(row._mapping) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > spec.per_page:
            novels.pop()
            last = novels[-1]
            if spec.order_by == "random":
                cursor_out = {"shuffle": last.shuffle, "id": last.id}
            else:
                cursor_out = {
                    "id": last.id,
                    spec.order_by: getattr(last, spec.order_by, None),
                }

        return {"novels": novels, "cursor": cursor_out}

    def count_novels(self, spec: QuerySpec) -> NovelCount:
        """Count VISIBLE novels matching *spec* (bounded)."""
        blocked_names = self._validate_and_resolve_blocked(spec)
        return self._count_with_spec(spec, blocked_tag_names=blocked_names)

    def _count_with_spec(
        self,
        spec: QuerySpec,
        *,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
        count_blocked: bool = False,
    ) -> NovelCount:
        """有界计数：``min(真实匹配数, COUNT_CAP)`` + 是否被截断。"""
        builder = NovelQueryBuilder(
            self, spec,
            restrict_ids=restrict_ids,
            blocked_tag_names=blocked_tag_names,
        )
        stmt = builder.build_count_bounded(
            COUNT_CAP, count_blocked=count_blocked,
        )
        matched = int(self.session.execute(stmt).scalar() or 0)
        if matched > COUNT_CAP:
            return NovelCount(COUNT_CAP, truncated=True)
        return NovelCount(matched)

    def count_excluded_novels(self, spec: QuerySpec) -> NovelCount:
        """Count novels matching *spec* that carry blocked tags (同样有界)."""
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return NovelCount(0)
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return NovelCount(0)
        return self._count_with_spec(
            spec, blocked_tag_names=blocked_names, count_blocked=True,
        )

    def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags."""
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return False
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return False
        builder = NovelQueryBuilder(
            self, spec, blocked_tag_names=blocked_names,
        )
        stmt = builder.build_existence(count_blocked=True)
        if stmt is None:
            return False
        return self.session.execute(stmt).first() is not None

    def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        blocked_names = self._validate_and_resolve_blocked(spec)
        builder = NovelQueryBuilder(
            self, spec, blocked_tag_names=blocked_names,
        )
        stmt = builder.build_ids()
        return list(self.session.execute(stmt).scalars())

    def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*."""
        if not novel_ids:
            return []
        blocked_names = self._validate_and_resolve_blocked(spec)
        builder = NovelQueryBuilder(self, spec)
        stmt = builder.build_ids_in_scope(
            novel_ids, blocked_tag_names=blocked_names,
        )
        return list(self.session.execute(stmt).scalars())

    def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order."""
        if not novel_ids:
            return []
        rows = self.session.execute(
            select(models.Novel).where(models.Novel.id.in_(novel_ids))
        ).scalars().all()
        novels = [_novel_from_orm(n) for n in rows]
        _attach_special_follow(self.session, novels)
        by_id = {n.id: n for n in novels}
        return [by_id[nid] for nid in novel_ids if nid in by_id]

    def _get_random_novels_shuffle(
        self, limit: int, min_likes: int, min_texts: int,
        blocked_tag_names: frozenset[str] = frozenset(),
    ) -> list[Novel]:
        """Return *limit* novels in shuffle order, starting from a random offset."""
        import random as _random

        blocked_clause = blocked_tags_excluded(blocked_tag_names)

        max_shuffle = self.session.scalar(
            select(func.coalesce(func.max(models.Novel.shuffle), 0)),
        ) or 0

        novels: list[Novel] = []
        start = _random.randint(0, max_shuffle) if max_shuffle > 0 else 0

        rows = self.session.execute(
            select(models.Novel)
            .where(
                models.Novel.like >= min_likes,
                models.Novel.text >= min_texts,
                models.Novel.shuffle >= start,
                *((blocked_clause,) if blocked_clause is not None else ()),
            )
            .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
            .limit(limit)
        ).scalars().all()
        for novel in rows:
            novels.append(_novel_from_orm(novel))

        if len(novels) < limit and start > 0:
            remaining = limit - len(novels)
            seen_ids = {n.id for n in novels}
            rows = self.session.execute(
                select(models.Novel)
                .where(
                    models.Novel.like >= min_likes,
                    models.Novel.text >= min_texts,
                    models.Novel.shuffle >= 0,
                    *((blocked_clause,) if blocked_clause is not None else ()),
                )
                .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
                .limit(remaining + len(seen_ids))
            ).scalars().all()
            for novel in rows:
                nd = _novel_from_orm(novel)
                if nd.id not in seen_ids:
                    novels.append(nd)
                    if len(novels) >= limit:
                        break

        _attach_special_follow(self.session, novels)
        return novels

    # ---- helpers -------------------------------------------------------------

    def _validate_query_field(self, field: str) -> None:
        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValidationError(f"Invalid query field: {field}")

    @staticmethod
    def _validate_order_direction(order_direction: str) -> None:
        if order_direction.upper() not in ("ASC", "DESC"):
            raise ValidationError(
                f"Invalid order_direction: {order_direction} (expected ASC/DESC)"
            )
