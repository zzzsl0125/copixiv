"""Search history API endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.search_history import SearchHistoryRepository
from copixiv.application.search_history import ListHistoryUseCase, DeleteHistoryUseCase

router = APIRouter()


@router.get("/")
async def get_search_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    use_case = ListHistoryUseCase(SearchHistoryRepository(db))
    return await use_case.execute(limit=limit, offset=offset)


@router.delete("/{history_id}")
async def delete_search_history(history_id: int, db: Session = Depends(get_db)):
    use_case = DeleteHistoryUseCase(SearchHistoryRepository(db))
    await use_case.execute(history_id)
    db.commit()
    return {"ok": True}
