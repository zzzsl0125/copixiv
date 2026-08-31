"""Search history repository."""

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select, delete as _delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemySearchHistoryRepository(BaseRepository):
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
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(models.SearchHistory)
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
        return self._delete_by_id(models.SearchHistory, history_id)

    async def clear_all(self) -> int:
        """Delete every search-history row. Returns the number deleted."""
        result = self.session.execute(_delete(models.SearchHistory))
        return result.rowcount or 0
