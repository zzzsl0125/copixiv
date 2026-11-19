"""Series repository for CRUD, aggregate refresh, orphan attach, and repair queries."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, update
from sqlalchemy import select as _select
from sqlalchemy.orm import Session

from copixiv.db import constants as C
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import (
    BaseRepository,
    ensure_parent_rows,
    finalize_parent_links,
    model_to_dict,
    update_summary,
)


def _retired_series_subquery():
    """Series IDs retired from pixiv — excluded from repair candidates."""
    return _select(models.SeriesDeadLetter.series_id).where(models.SeriesDeadLetter.retired_at.isnot(None))


class SQLAlchemySeriesRepository(BaseRepository):
    """Repository for series CRUD and statistics."""

    DRAFT_COLUMNS = (
        "id",
        "title",
        "author_id",
        "author_name",
        "path",
        "like",
        "view",
        "text",
        "caption",
        "series_id",
        "series_name",
        "series_index",
        "tags",
    )

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, series_ids: set[int]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING placeholder rows (FK safety)."""
        ensure_parent_rows(self.session, series_ids=series_ids)

    def get_by_id(self, series_id: int) -> dict | None:
        series = self.session.get(models.Series, series_id)
        if series is None:
            return None
        return model_to_dict(series)

    def get_name_fallback(self, series_id: int) -> str | None:
        """Look up series name from the ``series`` row or any novel carrying ``series_id``."""
        row = self.get_by_id(series_id)
        name = (row or {}).get("series_name")
        if name:
            return str(name)
        return self.session.execute(
            _select(func.max(models.Novel.series_name)).where(
                models.Novel.series_id == series_id,
                models.Novel.series_name.isnot(None),
            )
        ).scalar()

    def update_summary(self, series_ids: set[int] | None = None) -> None:
        """Recalculate series aggregates."""
        update_summary(
            self.session,
            models.Series,
            C.COL_SERIES_ID,
            series_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
                func.max(models.Novel.author_id).label(C.COL_AUTHOR_ID),
            ],
        )

    def update_series_name(self, series_id: int, name: str) -> None:
        """Write ``series.series_name`` if distinct from the current value."""
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

    def glue_orphan_novel(
        self,
        novel_id: int,
        *,
        series_id: int,
        series_name: str | None,
        series_index: int | None,
        author_id: int | None,
        force_series_index: bool = False,
    ) -> bool:
        """Attach an orphan-index novel row back onto *series_id* and update aggregates."""
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return False
        effective_author = novel.author_id or author_id
        if not effective_author:
            return False

        old_author_id = novel.author_id
        old_series_id = novel.series_id

        ensure_parent_rows(
            self.session,
            author_ids={effective_author},
            series_ids={series_id},
        )
        novel.author_id = effective_author
        novel.series_id = int(series_id)
        if series_name or novel.series_name:
            novel.series_name = series_name or novel.series_name
        if series_index is not None and (force_series_index or novel.series_index is None):
            novel.series_index = int(series_index)

        self.session.flush()
        engine_delta.record_novel_upserts(self.session, [novel_id])
        finalize_parent_links(
            self.session,
            author_ids={old_author_id, effective_author},
            series_ids={old_series_id, int(series_id)},
        )
        return True

    def count_novels_by_series(self, series_id: int) -> int:
        """Return how many novels currently point to *series_id*."""
        return int(
            self.session.execute(
                _select(func.count()).select_from(models.Novel).where(models.Novel.series_id == series_id)
            ).scalar_one()
        )

    def single_book_series_missing_index(self) -> list[int]:
        """Rows that are the only volume of their series and have no index."""
        return list(
            self.session.execute(
                _select(models.Novel.id).where(
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
            )
            .scalars()
            .all()
        )

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
            )
            .scalars()
            .all()
            if sid is not None
        ]

    @classmethod
    def _draft_columns(cls) -> list:
        return [getattr(models.Novel, name) for name in cls.DRAFT_COLUMNS]

    def novels_of_series(self, series_id: int) -> list[dict]:
        """Return column dicts for novels belonging to *series_id*, ordered by id."""
        return [
            dict(row._mapping)
            for row in self.session.execute(
                _select(*self._draft_columns())
                .where(models.Novel.series_id == series_id)
                .order_by(models.Novel.id)
            ).all()
        ]

    def novel_columns(self, novel_id: int) -> dict | None:
        """Return column dict for a single novel ID, or ``None``."""
        row = self.session.execute(
            _select(*self._draft_columns()).where(models.Novel.id == novel_id)
        ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def novels_with_orphan_index(
        self, limit: int | None = None, after_id: int = 0
    ) -> list[tuple[int, str | None, int | None]]:
        """Rows with ``series_id IS NULL`` and a stale ``series_index``."""
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
        return [(int(row[0]), row[1], row[2]) for row in self.session.execute(stmt).all()]

    def series_with_stored_index(self, after_id: int = 0, limit: int | None = None) -> list[int]:
        """Series that have at least one novel with a stored ``series_index``."""
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
        return [int(sid) for sid in self.session.execute(stmt).scalars().all() if sid is not None]
