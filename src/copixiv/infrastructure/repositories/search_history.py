"""Search history repository."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, delete as _delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from .base import BaseRepository


class SearchHistoryRepository(BaseRepository):
    """Repository for search history entries."""

    def __init__(self, session: Session):
        super().__init__(session)

    async def get_all(
        self, limit: int = 50, offset: int = 0
    ) -> Sequence[models.SearchHistory]:
        stmt = (
            select(models.SearchHistory)
            .order_by(models.SearchHistory.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    async def add_or_update(
        self,
        type_: str,
        value: str,
        display_value: str | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        stmt = (
            sqlite_insert(models.SearchHistory)
            .values(
                type=type_,
                value=value,
                display_value=display_value,
                timestamp=now,
            )
            .on_conflict_do_update(
                index_elements=["type", "value"],
                set_={"timestamp": now, "display_value": display_value},
            )
        )
        self.session.execute(stmt)

    async def delete(self, history_id: int) -> bool:
        entry = self.session.get(models.SearchHistory, history_id)
        if entry is None:
            return False
        self.session.delete(entry)
        return True
