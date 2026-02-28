from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional, Literal
import json
from pathlib import Path
from urllib.parse import quote

from web_api.deps import get_db
from core.db.repositories.novel import Novel
from core.db.models import Novel as NovelModel
from core.db import constants as C

router = APIRouter()

@router.get("/", response_model=dict)
def get_novels(
    db: Session = Depends(get_db),
    queries: Optional[str] = Query(None),
    order_by: str = C.ORDER_BY_RANDOM, 
    order_direction: str = "DESC",
    cursor: Optional[str] = None,
    per_page: int = 20,
    min_like: Optional[int] = None,
    min_text: Optional[int] = None,
):
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

def get_novel_or_404(novel_id: int, db: Session = Depends(get_db)):
    novel = db.get(NovelModel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel

@router.post("/{novel_id}/favourite", status_code=204)
async def toggle_favourite(
    novel_id: int,
    db_session: Session = Depends(get_db)
):
    novel_repo = Novel(db_session)
    novel_repo.toggle_favourite(novel_id)
    db_session.commit()
    return

@router.post("/author/{author_id}/follow", status_code=204)
async def toggle_special_follow(
    author_id: int,
    db_session: Session = Depends(get_db)
):
    novel_repo = Novel(db_session)
    novel_repo.toggle_special_follow(author_id)
    db_session.commit()
    return

@router.get("/{novel_id}/download")
async def download_novel(
    novel: NovelModel = Depends(get_novel_or_404),
    db_session: Session = Depends(get_db),
    format: Literal['txt', 'epub'] = 'txt'
):
    if not novel.path:
        raise HTTPException(
            status_code=404, 
            detail=f"Novel#{novel.id} without path."
        )

    file_path = Path(novel.path).with_suffix('.'+format)
    media_type = 'text/plain' if format == 'txt' else 'application/epub+zip'

    if not file_path.is_file():
        raise HTTPException(
            status_code=404, 
            detail=f"Novel#{novel.id} not found."
        )

    headers = {
        'Content-Disposition': \
            f"attachment; filename*=UTF-8''{quote(file_path.name)}"
    }
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers=headers
    )

@router.delete("/{novel_id}", status_code=204)
async def delete_novel(
    novel: NovelModel = Depends(get_novel_or_404),
    db_session: Session = Depends(get_db)
):
    if novel.path:
        if Path(novel.path).is_file():
            try:
                Path(novel.path).unlink()
            except OSError:
                pass
        
    novel_repo = Novel(db_session)
    novel_repo.delete(novel.id)
    db_session.commit()
    return
