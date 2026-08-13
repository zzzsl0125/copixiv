"""Search history API endpoints."""

from fastapi import APIRouter, Depends, Query

from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.application.search_history import (
    ClearHistoryUseCase, DeleteHistoryUseCase, ListHistoryUseCase,
)

router = APIRouter()


@router.get("/")
async def get_search_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    use_case = ListHistoryUseCase(uow.search_history)
    return await use_case.execute(limit=limit, offset=offset)


@router.delete("/")
async def clear_search_history(uow: SqlUnitOfWork = Depends(get_uow)):
    """Delete all search-history entries (frontend "全部清除" button)."""
    deleted = await ClearHistoryUseCase(uow.search_history).execute()
    return {"deleted": deleted}


@router.delete("/{history_id}")
async def delete_search_history(history_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = DeleteHistoryUseCase(uow.search_history)
    await use_case.execute(history_id)
    return {"ok": True}
