# -*- coding: utf-8 -*-
from sqlalchemy import update as _update, delete as _delete, select as _select

from .base_repository import BaseRepository
from .. import constants as C
from .. import models

class Series(BaseRepository):
    def get_by_id(self, item_id: int) -> models.Series | None:
        return super().get_by_id(models.Series, item_id)

    def get_summary_item(self, item_id: int) -> dict | None:
        return super().get_summary_item(models.Series, item_id)
    
    def update_summary(self, series_ids: list[int] | int | None = None):
        from sqlalchemy import func
        super()._update_summary(
            models.Series, C.COL_SERIES_ID, series_ids,
            extra_columns=[
                func.max(models.Novel.author_id).label(C.COL_AUTHOR_ID),
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME)
            ]
        )
    
    def get_empty_series_ids(self) -> list[int]:
        return self.session.execute(
            _select(models.Series.series_id)
            .where(models.Series.series_name.is_(None))
            .distinct()
        ).scalars().all()
    
    def delete_series_and_data(self, series_id: int):
        novel_ids = self.session.execute(
            _select(models.Novel.id)
            .where(models.Novel.series_id == series_id)
        ).scalars().all()
        
        from core.db.database import db
        novel_repo = db.novel(self.session)
        for nid in novel_ids:
            novel_repo.delete(nid)
            
        self.session.execute(
            _delete(models.Series)
            .where(models.Series.series_id == series_id)
        )

    def series_with_empty_index(self) -> list[int]:
        return self.session.execute(
            _select(models.Novel.series_id)
            .where(models.Novel.series_id.is_not(None))
            .where(models.Novel.series_index.is_(None))
        ).scalars().all()


    
