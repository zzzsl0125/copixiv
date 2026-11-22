"""Series repository for CRUD, aggregate queries, and chapter-index repair queries."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, update
from sqlalchemy import select as _select
from sqlalchemy.orm import Session

from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import model_to_dict


def _retired_series_subquery():
    """Series IDs retired from pixiv — excluded from repair candidates."""
    return _select(models.SeriesDeadLetter.series_id).where(models.SeriesDeadLetter.retired_at.isnot(None))


class SQLAlchemySeriesRepository:
    """Repository for series CRUD and statistics."""

    DRAFT_COLUMNS = (
        "id",
        "author_id",
        "series_index",
    )

    def __init__(self, session: Session):
        self.session = session

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
