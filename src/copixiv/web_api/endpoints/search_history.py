"""Search history API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.search_history import SearchHistoryRepository

router = APIRouter()


@router.get("/")
async def get_search_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    repo = SearchHistoryRepository(db)
    return await repo.get_all(limit=limit, offset=offset)


@router.delete("/{history_id}")
async def delete_search_history(history_id: int, db: Session = Depends(get_db)):
    repo = SearchHistoryRepository(db)
    if not await repo.delete(history_id):
        raise HTTPException(status_code=404)
    return {"ok": True}
