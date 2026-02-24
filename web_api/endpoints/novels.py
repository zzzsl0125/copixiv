from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
import json

from web_api.deps import get_db
from core.db.repositories.novel import Novel
from core.db import constants as C

router = APIRouter()

@router.get("/", response_model=List[dict])
def get_novels(
    db: Session = Depends(get_db),
    queries: Optional[str] = Query(None),
    order_by: str = C.COL_LIKES, 
    order_direction: str = "DESC",
    cursor: Optional[str] = None,
    per_page: int = 20,
    min_like: Optional[int] = None,
    min_text: Optional[int] = None,
):
    """
    Get a list of novels based on filters.
    Pass 'queries' as a URL-encoded JSON string.
    Example: ?queries=%7B%22keyword%22%3A%22test%22%7D
    """
    novel_repo = Novel(db)
    
    try:
        if queries:
            queries = json.loads(queries)
        if cursor:
            cursor = json.loads(cursor)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"Invalid JSON format")
    
    results = novel_repo.get_novels(
        queries=queries,
        order_by=order_by,
        order_direction=order_direction,
        cursor=cursor,
        per_page=per_page,
        min_like=min_like,
        min_text=min_text,
    )
    
    return results
