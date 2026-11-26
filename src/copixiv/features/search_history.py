"""Search history slice: repository, recording service, and API router."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as _delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from copixiv.core.exceptions import NotFoundError
from copixiv.core.search import SearchConditions
from copixiv.db import models
from copixiv.db.base import delete_by_id
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.log import logger

# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SQLAlchemySearchHistoryRepository:
    """Repository for search history entries."""

    def __init__(self, session: Session):
        self.session = session

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
        return delete_by_id(self.session, models.SearchHistory, history_id)

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


# ---------------------------------------------------------------------------
# Recording helper (FastAPI BackgroundTasks)
# ---------------------------------------------------------------------------


def record_search_history(
    conditions: SearchConditions,
    uow_factory: Callable[[], SqlUnitOfWork],
) -> None:
    """Record search history entries for a list of search conditions."""
    if not conditions:
        return
    uow = uow_factory()
    try:
        run_write_transaction_sync(uow, lambda tx: _record_in_transaction(tx, conditions))
    except Exception:
        logger.exception("Failed to record search history")


_SIGNED_INT_RE = re.compile(r"-?\d+", re.ASCII)


def _parse_entity_id(value: str) -> int | None:
    """Parse a signed decimal entity ID string, returning ``None`` if invalid."""
    if not _SIGNED_INT_RE.fullmatch(value):
        return None
    return int(value)


def _record_in_transaction(uow: SqlUnitOfWork, conditions: SearchConditions) -> None:
    author_repo = SQLAlchemyAuthorRepository(uow.session)
    series_repo = SQLAlchemySeriesRepository(uow.session)
    history_repo = SQLAlchemySearchHistoryRepository(uow.session)
    for qtype, value in conditions:
        display_value = None
        if qtype == "author_id":
            parsed_id = _parse_entity_id(value)
            if parsed_id is not None:
                author = author_repo.get_by_id(parsed_id)
                if author:
                    display_value = author.get("author_name")
        elif qtype == "series_id":
            parsed_id = _parse_entity_id(value)
            if parsed_id is not None:
                series = series_repo.get_by_id(parsed_id)
                if series:
                    display_value = series.get("series_name")
        history_repo.add_or_update(qtype, value, display_value)
    history_repo.prune_old(keep=50)


# ---------------------------------------------------------------------------
# Schemas & HTTP API
# ---------------------------------------------------------------------------

router = APIRouter()


class SearchHistoryResponse(BaseModel):
    id: int
    type: str
    value: str
    display_value: str | None = None
    timestamp: datetime
    model_config = ConfigDict(from_attributes=True)


@router.get("", response_model=list[SearchHistoryResponse])
def get_search_history(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    with uow.begin(read_only=True):
        rows = SQLAlchemySearchHistoryRepository(uow.session).get_all(limit=limit, offset=offset)
        return [SearchHistoryResponse.model_validate(row) for row in rows]


@router.delete("", status_code=204)
def clear_search_history(uow: UowDep):
    """Delete all search-history entries (frontend "全部清除" button)."""
    run_write_transaction_sync(uow, lambda tx: SQLAlchemySearchHistoryRepository(tx.session).clear_all())


@router.delete("/{history_id}", status_code=204)
def delete_search_history(history_id: int, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        if not SQLAlchemySearchHistoryRepository(tx.session).delete(history_id):
            raise NotFoundError(f"Search history {history_id} not found")

    run_write_transaction_sync(uow, _write)
