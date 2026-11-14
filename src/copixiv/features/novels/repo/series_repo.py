"""Series repository for CRUD, aggregate refresh, and repair candidate queries."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, update
from sqlalchemy import select as _select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import constants as C
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository, model_to_dict, update_summary

from .query_builder import _retired_series_subquery


class SQLAlchemySeriesRepository(BaseRepository):
    """Repository for series CRUD and statistics."""

    # 造 NovelDraft 要用的列，与 ``tasks/maintenance/series.py::_draft_from_row``
    # 一一对应（列名 = NovelDraft 字段名）。
    DRAFT_COLUMNS = (
        "id", "title", "author_id", "author_name", "path", "like", "view",
        "text", "caption", "series_id", "series_name", "series_index", "tags",
    )

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, series_ids: set[int]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING placeholder rows (FK safety)."""
        if not series_ids:
            return
        self.session.execute(
            pg_insert(models.Series)
            .values([{"series_id": sid} for sid in sorted(series_ids)])
            .on_conflict_do_nothing()
        )
        self.session.flush()

    def get_by_id(self, series_id: int) -> dict | None:
        series = self.session.get(models.Series, series_id)
        if series is None:
            return None
        return model_to_dict(series)

    def update_summary(self, series_ids: set[int] | None = None) -> None:
        """Recalculate series aggregates."""
        update_summary(
            self.session, models.Series, C.COL_SERIES_ID, series_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
                func.max(models.Novel.author_id).label(C.COL_AUTHOR_ID),
            ],
        )

    def update_series_name(self, series_id: int, name: str) -> None:
        """写入 ``series`` 行的名字（编辑器的解析结果）。"""
        self.session.execute(
            update(models.Series)
            .where(
                models.Series.series_id == series_id,
                models.Series.series_name.is_distinct_from(name),
            )
            .values(series_name=name)
        )

    def update_series_indices(self, updates: Mapping[int, int | None]) -> None:
        """Update ``series_index`` in bulk by primary key for existing novels."""
        if not updates:
            return
        self.session.execute(
            update(models.Novel),
            [{"id": nid, "series_index": idx} for nid, idx in updates.items()],
        )
        engine_delta.record_novel_upserts(self.session, updates.keys())

    def count_novels_by_series(self, series_id: int) -> int:
        """库内已有多少本挂在这个 series_id 下（预览里的"库内已有 N 本"）。"""
        return int(self.session.execute(
            _select(func.count())
            .select_from(models.Novel)
            .where(models.Novel.series_id == series_id)
        ).scalar_one())

    def single_book_series_missing_index(self) -> list[int]:
        """Rows that are the only volume of their series and have no index."""
        return list(self.session.execute(
            _select(models.Novel.id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_index.is_(None),
                ~models.Novel.series_id.in_(_retired_series_subquery()),
                ~models.Novel.series_id.in_(
                    _select(models.Novel.series_id)
                    .where(models.Novel.series_id.isnot(None))
                    .group_by(models.Novel.series_id)
                    .having(func.count(models.Novel.id) > 1)
                ),
            )
        ).scalars().all())

    def series_with_empty_index(self) -> list[int]:
        """Series with at least one chapter number missing."""
        return [
            int(sid)
            for sid in self.session.execute(
                _select(models.Novel.series_id)
                .where(
                    models.Novel.series_id.isnot(None),
                    models.Novel.series_index.is_(None),
                    ~models.Novel.series_id.in_(_retired_series_subquery()),
                )
                .distinct()
            ).scalars().all()
            if sid is not None
        ]

    @classmethod
    def _draft_columns(cls) -> list:
        return [getattr(models.Novel, name) for name in cls.DRAFT_COLUMNS]

    def novels_of_series(self, series_id: int) -> list[dict]:
        """库内挂在该系列下的小说（全套现值列，按 ``id`` 升序）。"""
        return [
            dict(row._mapping)
            for row in self.session.execute(
                _select(*self._draft_columns())
                .where(models.Novel.series_id == series_id)
                .order_by(models.Novel.id)
            ).all()
        ]

    def novel_columns(self, novel_id: int) -> dict | None:
        """单行的"造 draft"现值列（孤儿分支逐本用），没有这一行则 None。"""
        row = self.session.execute(
            _select(*self._draft_columns()).where(models.Novel.id == novel_id)
        ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def novels_with_orphan_index(
        self, limit: int | None = None, after_id: int = 0,
    ) -> list[tuple[int, str | None, int | None]]:
        """``series_id`` 为空、``series_index`` 却还留着的"孤儿号"行。"""
        stmt = (
            _select(
                models.Novel.id,
                models.Novel.series_name,
                models.Novel.series_index,
            )
            .where(
                models.Novel.series_id.is_(None),
                models.Novel.series_index.isnot(None),
                models.Novel.id > after_id,
            )
            .order_by(models.Novel.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            (int(row[0]), row[1], row[2])
            for row in self.session.execute(stmt).all()
        ]

    def series_with_stored_index(
        self, after_id: int = 0, limit: int | None = None,
    ) -> list[int]:
        """至少有一本带号的系列（``fix_series_index`` 全量复查模式的候选）。"""
        stmt = (
            _select(models.Novel.series_id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_id > after_id,
                ~models.Novel.series_id.in_(_retired_series_subquery()),
            )
            .group_by(models.Novel.series_id)
            .having(func.count(models.Novel.series_index) > 0)
            .order_by(models.Novel.series_id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            int(sid)
            for sid in self.session.execute(stmt).scalars().all()
            if sid is not None
        ]
