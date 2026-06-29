"""Series repository."""

from sqlalchemy import select as _select, func
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from .base import BaseRepository


class SeriesRepository(BaseRepository):
    """Repository for series CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    async def get_by_id(self, series_id: int) -> dict | None:
        series = self.session.get(models.Series, series_id)
        if series is None:
            return None
        return {c.name: getattr(series, c.name) for c in series.__table__.columns}

    async def update_summary(self, series_ids: set[int] | None = None) -> None:
        super()._update_summary(
            models.Series, C.COL_SERIES_ID, series_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
            ],
        )

    async def get_empty_series_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Series.series_id)
            .where(models.Series.series_name.is_(None))
            .distinct()
        ).scalars().all())

    async def series_with_empty_index(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Novel.series_id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_index.is_(None),
            )
            .distinct()
        ).scalars().all())
