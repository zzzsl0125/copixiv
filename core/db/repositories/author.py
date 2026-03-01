# -*- coding: utf-8 -*-

from sqlalchemy import update as _update, delete as _delete, select as _select

from .base_repository import BaseRepository
from .. import constants as C
from .. import models

class Author(BaseRepository):

    def get_summary_item(self, item_id: int) -> dict | None:
        return super().get_summary_item(models.Author, item_id)
    
    def update_summary(self, author_ids: list[int] | int | None = None):
        super()._update_summary(models.Author, C.COL_AUTHOR_ID, author_ids)

    def update_author_name(self, author_id: int, author_name: str):
        self.session.execute(
            _update(models.Novel)
            .where(models.Novel.author_id == author_id)
            .values(author_name=author_name)
        )
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(author_name=author_name)
        )

    def delete_author_and_data(self, author_id: int):
        novel_ids = self.session.execute(
            _select(models.Novel.id)
            .where(models.Novel.author_id == author_id)
        ).scalars().all()
        
        from core.db.database import db
        novel_repo = db.novel(self.session)
        for nid in novel_ids:
            novel_repo.delete(nid)
            
        self.session.execute(
            _delete(models.Series)
            .where(models.Series.author_id == author_id)
        )
        
        self.session.execute(
            _delete(models.Author)
            .where(models.Author.author_id == author_id)
        )

    def get_empty_author_ids(self) -> list[int]:
        return self.session.execute(
            _select(models.Novel.author_id)
            .where(models.Novel.author_name.is_(None))
            .distinct()
        ).scalars().all()

    def get_author_name(self, author_id: int) -> str | None:
        return self.session.execute(
            _select(models.Author.author_name)
            .where(models.Author.author_id == author_id)
        ).scalar_one_or_none()

