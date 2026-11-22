"""Search history API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.search_history.repo import SQLAlchemySearchHistoryRepository

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
