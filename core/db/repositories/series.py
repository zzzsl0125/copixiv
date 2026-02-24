# -*- coding: utf-8 -*-
from .base_repository import BaseRepository
from .. import constants as C
from .. import models

class Series(BaseRepository):
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

    
