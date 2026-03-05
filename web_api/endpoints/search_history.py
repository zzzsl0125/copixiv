# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session
from typing import List

from web_api.deps import get_db
from core.db.repositories.search_history import SearchHistoryRepository
from web_api import schemas

router = APIRouter()

@router.get("/", response_model=List[schemas.SearchHistoryResponse])
def get_search_history(
    db: Session = Depends(get_db)
):
    """
    Retrieve the most recent search history.
    """
    repo = SearchHistoryRepository(db)
    history = repo.get_all(limit=20)
    return history

@router.delete("/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_search_history_item(
    history_id: int,
    db: Session = Depends(get_db)
):
    """
    Delete a single search history item.
    """
    repo = SearchHistoryRepository(db)
    repo.delete(history_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
def clear_all_search_history(
    db: Session = Depends(get_db)
):
    """
    Clear all search history.
    """
    repo = SearchHistoryRepository(db)
    repo.delete_all()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
