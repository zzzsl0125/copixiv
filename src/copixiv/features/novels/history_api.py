"""Search history API endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.deps import get_uow, get_write_uow
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.history_repo import SQLAlchemySearchHistoryRepository

router = APIRouter()


# ---------------------------------------------------------------------------
# Search-history schemas — carried with the feature (S1).
# ---------------------------------------------------------------------------

class SearchHistoryResponse(BaseModel):
    id: int
    type: str
    value: str
    display_value: str | None = None
    # timestamptz → datetime; Pydantic serializes to an ISO string on the wire.
    timestamp: datetime
    model_config = ConfigDict(from_attributes=True)


@router.get("/", response_model=list[SearchHistoryResponse])
async def get_search_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    return await SQLAlchemySearchHistoryRepository(uow.session).get_all(limit=limit, offset=offset)


@router.delete("/")
async def clear_search_history(uow: SqlUnitOfWork = Depends(get_write_uow)):
    """Delete all search-history entries (frontend "全部清除" button)."""
    deleted = await SQLAlchemySearchHistoryRepository(uow.session).clear_all()
    return {"deleted": deleted}


@router.delete("/{history_id}")
async def delete_search_history(history_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await SQLAlchemySearchHistoryRepository(uow.session).delete(history_id):
        raise NotFoundError(f"Search history {history_id} not found")
    return {"ok": True}
