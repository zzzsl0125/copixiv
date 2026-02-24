# -*- coding: utf-8 -*-

from .base_repository import BaseRepository
from .. import constants as C
from .. import models

class Author(BaseRepository):

    def get_summary_item(self, item_id: int) -> dict | None:
        return super().get_summary_item(models.Author, item_id)
    
    def update_summary(self, author_ids: list[int] | int | None = None):
        super()._update_summary(models.Author, C.COL_AUTHOR_ID, author_ids)
