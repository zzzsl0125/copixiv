"""Novel API endpoints — identical contract to v1."""

from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, HTTPException, Body
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db, parse_queries_json, parse_json_cursor
from copixiv.web_api.schemas import BatchDownloadRequest
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.repositories.author import AuthorRepository
from copixiv.infrastructure.repositories.series import SeriesRepository
from copixiv.infrastructure.repositories.search_history import SearchHistoryRepository
from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from copixiv.domain.services.archive import build_batch_zip
from copixiv.infrastructure.storage.file_storage import FileStorage

import logging
logger = logging.getLogger("copixiv")

router = APIRouter()


@router.get("/", response_model=dict)
def get_novels(
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
    queries: str | None = Query(None),
    order_by: str = C.ORDER_BY_RANDOM,
    order_direction: str = "DESC",
    cursor: str | None = None,
    per_page: int = 20,
    min_like: int | None = None,
    min_text: int | None = None,
):
    queries_dict = parse_queries_json(queries)
    cursor_dict = parse_json_cursor(cursor)

    repo = NovelRepository(db)
    import asyncio
    results = asyncio.run(repo.get_novels(
        queries=queries_dict,
        order_by=order_by,
        order_direction=order_direction,
        cursor=cursor_dict,
        per_page=per_page,
        min_like=min_like,
        min_text=min_text,
    ))

    if queries_dict and background_tasks:
        def _record_history():
            from copixiv.infrastructure.database.engine import create_session_factory
            repo = SearchHistoryRepository(db)
            for value, qtype in queries_dict.items():
                display_value = None
                if qtype == "author_id":
                    author = AuthorRepository(db).get_by_id(int(value))
                    if author:
                        display_value = author.get("author_name")
                elif qtype == "series_id":
                    series = SeriesRepository(db).get_by_id(int(value))
                    if series:
                        display_value = series.get("series_name")
                import asyncio as a
                a.run(repo.add_or_update(qtype, value, display_value))
        background_tasks.add_task(_record_history)

    return results


@router.get("/count")
def count_novels(
    db: Session = Depends(get_db),
    queries: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
):
    import asyncio
    repo = NovelRepository(db)
    total = asyncio.run(repo.count_novels(
        queries=parse_queries_json(queries),
        min_like=min_like,
        min_text=min_text,
    ))
    return {"total": total}


def _get_novel_or_404(novel_id: int, db: Session):
    novel = db.get(models.Novel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel


@router.post("/{novel_id}/favourite", status_code=204)
def toggle_favourite(
    novel_id: int,
    db_session: Session = Depends(get_db),
):
    import asyncio
    repo = NovelRepository(db_session)
    asyncio.run(repo.toggle_favourite(novel_id))
    db_session.commit()


@router.post("/author/{author_id}/follow", status_code=204)
def toggle_special_follow(
    author_id: int,
    db_session: Session = Depends(get_db),
):
    import asyncio
    repo = NovelRepository(db_session)
    asyncio.run(repo.toggle_special_follow(author_id))
    db_session.commit()


@router.get("/{novel_id}/download")
def download_novel(
    novel_id: int,
    db_session: Session = Depends(get_db),
    format: Literal["txt", "epub"] = "txt",
):
    novel = _get_novel_or_404(novel_id, db_session)
    if not novel.path:
        raise HTTPException(status_code=404, detail=f"Novel#{novel.id} without path.")

    file_path = Path(novel.path).with_suffix("." + format)
    media_type = "text/plain" if format == "txt" else "application/epub+zip"

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Novel#{novel.id} not found.")

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_path.name)}"
    }
    return FileResponse(path=str(file_path), media_type=media_type, headers=headers)


@router.post("/batch-download")
def batch_download_novels(
    body: BatchDownloadRequest = Body(...),
    db: Session = Depends(get_db),
):
    import asyncio
    repo = NovelRepository(db)
    queries = parse_queries_json(body.queries)

    results = asyncio.run(repo.get_novels(
        queries=queries,
        order_by=body.order_by,
        order_direction=body.order_direction,
        per_page=body.limit,
        min_like=body.min_like,
        min_text=body.min_text,
    ))

    novels = results.get("novels", [])
    if not novels:
        raise HTTPException(status_code=404, detail="未找到匹配条件的小说")

    zip_buffer, titles, missing_ids = build_batch_zip(novels, body.format_mode)
    if not titles:
        raise HTTPException(status_code=404, detail="未找到可下载的有效文件")

    zip_buffer.seek(0)

    search_desc = f"批量下载_{len(titles)}篇"
    if queries:
        keywords = [k for k, v in queries.items() if v == "keyword"]
        if keywords:
            search_desc = f"{'_'.join(keywords[:3])}_{len(titles)}篇"

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(search_desc + '.zip')}",
    }
    if missing_ids:
        headers["X-Batch-Missing-Ids"] = ",".join(missing_ids)

    return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)


@router.delete("/{novel_id}", status_code=204)
def delete_novel(
    novel_id: int,
    db_session: Session = Depends(get_db),
):
    import asyncio
    novel = _get_novel_or_404(novel_id, db_session)
    storage = FileStorage()
    if novel.path:
        storage.delete_novel_files(novel.path)

    repo = NovelRepository(db_session)
    asyncio.run(repo.delete(novel.id))
    db_session.commit()
