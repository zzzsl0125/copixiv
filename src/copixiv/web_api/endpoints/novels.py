"""Novel API endpoints — identical contract to v1."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Body, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db, parse_queries_json, parse_json_cursor
from copixiv.web_api.schemas import BatchDownloadRequest
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.database import constants as C
from copixiv.application.search_history.record import record_search_history
from copixiv.application.novel import (
    BatchDownloadUseCase,
    CountNovelsUseCase,
    DeleteNovelUseCase,
    GetNovelFileUseCase,
    ListNovelsUseCase,
    ListNovelsRequest,
    ToggleFavouriteUseCase,
    ToggleSpecialFollowUseCase,
)

router = APIRouter()


@router.get("/", response_model=dict)
async def get_novels(
    request: Request,
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

    use_case = ListNovelsUseCase(NovelRepository(db))
    results = await use_case.execute(ListNovelsRequest(
        queries=queries_dict, order_by=order_by, order_direction=order_direction,
        cursor=cursor_dict, per_page=per_page, min_like=min_like, min_text=min_text,
    ))

    if queries_dict and background_tasks:
        background_tasks.add_task(
            record_search_history, queries_dict, request.app.state.session_factory,
        )

    return results


@router.get("/count")
async def count_novels(
    db: Session = Depends(get_db),
    queries: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
):
    use_case = CountNovelsUseCase(NovelRepository(db))
    total = await use_case.execute(
        queries=parse_queries_json(queries),
        min_like=min_like, min_text=min_text,
    )
    return {"total": total}


@router.post("/{novel_id}/favourite", status_code=204)
async def toggle_favourite(novel_id: int, db_session: Session = Depends(get_db)):
    use_case = ToggleFavouriteUseCase(NovelRepository(db_session))
    await use_case.execute(novel_id)
    db_session.commit()


@router.post("/author/{author_id}/follow", status_code=204)
async def toggle_special_follow(author_id: int, db_session: Session = Depends(get_db)):
    use_case = ToggleSpecialFollowUseCase(NovelRepository(db_session))
    await use_case.execute(author_id)
    db_session.commit()


@router.get("/{novel_id}/download")
async def download_novel(
    novel_id: int,
    db_session: Session = Depends(get_db),
    format: Literal["txt", "epub"] = "txt",
):
    use_case = GetNovelFileUseCase(NovelRepository(db_session))
    file_path, media_type = await use_case.execute(novel_id, format)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_path.name)}"
    }
    return FileResponse(path=str(file_path), media_type=media_type, headers=headers)


@router.post("/batch-download")
async def batch_download_novels(
    body: BatchDownloadRequest = Body(...),
    db: Session = Depends(get_db),
    request: Request = None,
):
    queries = parse_queries_json(body.queries)
    naming = request.app.state.config.batch_download.naming if request else None
    use_case = BatchDownloadUseCase(NovelRepository(db), naming)
    result = await use_case.execute(body, queries)

    headers = {
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(result.search_desc + '.zip')}"
        ),
    }
    if result.missing_ids:
        headers["X-Batch-Missing-Ids"] = ",".join(result.missing_ids)

    return Response(
        content=result.zip_buffer.getvalue(),
        media_type="application/zip",
        headers=headers,
    )


@router.delete("/{novel_id}", status_code=204)
async def delete_novel(
    novel_id: int,
    db_session: Session = Depends(get_db),
    request: Request = None,
):
    use_case = DeleteNovelUseCase(
        NovelRepository(db_session), request.app.state.file_storage
    )
    await use_case.execute(novel_id)
    db_session.commit()
