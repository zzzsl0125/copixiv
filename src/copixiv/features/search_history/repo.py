"""Search history repository."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete as _delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemySearchHistoryRepository(BaseRepository):
    """Repository for search history entries."""

    def __init__(self, session: Session):
        super().__init__(session)

    def get_all(self, limit: int = 50, offset: int = 0) -> Sequence[models.SearchHistory]:
        stmt = (
            select(models.SearchHistory)
            .order_by(models.SearchHistory.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def add_or_update(
        self,
        type_: str,
        value: str,
        display_value: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
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

    def delete(self, history_id: int) -> bool:
        return self._delete_by_id(models.SearchHistory, history_id)

    def clear_all(self) -> int:
        """Delete every search-history row. Returns the number deleted."""
        result = self.session.execute(_delete(models.SearchHistory))
        return cast(CursorResult[Any], result).rowcount or 0

    def prune_old(self, keep: int = 50) -> int:
        """Delete oldest rows beyond the latest *keep* entries. Returns deleted count."""
        keep_ids = (
            select(models.SearchHistory.id)
            .order_by(models.SearchHistory.timestamp.desc(), models.SearchHistory.id.desc())
            .limit(keep)
        )
        stmt = _delete(models.SearchHistory).where(models.SearchHistory.id.not_in(keep_ids))
        result = self.session.execute(stmt)
        return cast(CursorResult[Any], result).rowcount or 0
