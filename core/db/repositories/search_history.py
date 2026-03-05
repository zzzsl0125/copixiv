# -*- coding: utf-8 -*-
from sqlalchemy import select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from datetime import datetime, timezone

from .base_repository import BaseRepository
from .. import models

class SearchHistoryRepository(BaseRepository):
    def add_or_update(self, type: str, value: str, display_value: str = None):
        """
        Adds a new search history entry. If it already exists, updates the timestamp.
        """
        if not type or not value:
            return

        stmt = sqlite_insert(models.SearchHistory).values(
            type=type,
            value=value,
            display_value=display_value,
            timestamp=datetime.now(timezone.utc).isoformat()
        )

        update_stmt = stmt.on_conflict_do_update(
            index_elements=['type', 'value'],
            set_={
                'timestamp': stmt.excluded.timestamp,
                'display_value': stmt.excluded.display_value
            }
        )
        self.session.execute(update_stmt)

    def get_all(self, limit: int = 20) -> list[models.SearchHistory]:
        """
        Retrieves the most recent search history entries.
        """
        stmt = select(models.SearchHistory).order_by(models.SearchHistory.timestamp.desc()).limit(limit)
        result = self.session.execute(stmt)
        return result.scalars().all()

    def delete(self, history_id: int):
        """
        Deletes a specific search history entry by its ID.
        """
        stmt = delete(models.SearchHistory).where(models.SearchHistory.id == history_id)
        self.session.execute(stmt)

    def delete_all(self):
        """
        Deletes all search history entries.
        """
        stmt = delete(models.SearchHistory)
        self.session.execute(stmt)
