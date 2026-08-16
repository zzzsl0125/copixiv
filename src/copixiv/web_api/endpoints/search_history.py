"""Search history API endpoints."""

from fastapi import APIRouter, Depends, Query

from copixiv.domain.exceptions import NotFoundError
from copixiv.web_api.deps import get_uow, get_write_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork

router = APIRouter()


# Route manifest — mounted automatically by the composition root
# (docs/MODULARITY.md §M9): (prefix, tags) travels with the module.
ROUTE = ("/api/search-history", ["search_history"])



@router.get("/")
async def get_search_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    return await uow.search_history.get_all(limit=limit, offset=offset)


@router.delete("/")
async def clear_search_history(uow: SqlUnitOfWork = Depends(get_write_uow)):
    """Delete all search-history entries (frontend "全部清除" button)."""
    deleted = await uow.search_history.clear_all()
    return {"deleted": deleted}


@router.delete("/{history_id}")
async def delete_search_history(history_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await uow.search_history.delete(history_id):
        raise NotFoundError(f"Search history {history_id} not found")
    return {"ok": True}
