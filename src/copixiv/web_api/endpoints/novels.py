"""Novel API endpoints — identical contract to v1."""

from collections.abc import Iterator
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Body, Request
from fastapi.responses import FileResponse, StreamingResponse

from copixiv.web_api.deps import get_uow, get_write_uow, parse_json_param
from copixiv.domain.services.parsing import parse_search_keyword
from copixiv.web_api.schemas import BatchDownloadRequest, NovelListResponse
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database import constants as C
from copixiv.application.search_history.record import record_search_history
from copixiv.application.novel import (
    BatchDownloadUseCase,
    DeleteNovelUseCase,
    GetNovelFileUseCase,
)

router = APIRouter()


def _iter_zip(buffer) -> Iterator[bytes]:
    """Stream a ZIP buffer in chunks, closing it when done."""
    try:
        while chunk := buffer.read(1 << 16):
            yield chunk
    finally:
        buffer.close()


@router.get("/", response_model=NovelListResponse)
async def get_novels(
    request: Request,
    background_tasks: BackgroundTasks,
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    order_by: str = C.ORDER_BY_RANDOM,
    order_direction: str = "DESC",
    cursor: str | None = None,
    per_page: int = Query(20, ge=1, le=200),
    min_like: int | None = None,
    min_text: int | None = None,
):
    conditions = parse_search_keyword(keyword) if keyword else None
    cursor_dict = parse_json_param(cursor, "cursor")

    results = await uow.novels.get_novels(
        conditions=conditions,
        order_by=order_by,
        order_direction=order_direction,
        cursor=cursor_dict,
        per_page=per_page,
        min_like=min_like,
        min_text=min_text,
    )

    if conditions and background_tasks:
        background_tasks.add_task(
            record_search_history, conditions, request.app.state.session_factory,
        )

    return results


@router.get("/count")
async def count_novels(
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
):
    total = await uow.novels.count_novels(
        conditions=parse_search_keyword(keyword) if keyword else None,
        min_like=min_like, min_text=min_text,
    )
    return {"total": total}


@router.post("/{novel_id}/favourite", status_code=204)
async def toggle_favourite(novel_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    await uow.novels.toggle_favourite(novel_id)


@router.post("/author/{author_id}/follow", status_code=204)
async def toggle_special_follow(author_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    await uow.novels.toggle_special_follow(author_id)


@router.get("/{novel_id}/download")
async def download_novel(
    request: Request,
    novel_id: int,
    uow: SqlUnitOfWork = Depends(get_uow),
    format: Literal["txt", "epub"] = "txt",
):
    use_case = GetNovelFileUseCase(
        uow.novels, request.app.state.file_storage.download_dir,
    )
    file_path, media_type = await use_case.execute(novel_id, format)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_path.name)}"
    }
    return FileResponse(path=str(file_path), media_type=media_type, headers=headers)


@router.post("/batch-download")
async def batch_download_novels(
    request: Request,
    body: BatchDownloadRequest = Body(...),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = request.app.state.config.batch_download.naming
    use_case = BatchDownloadUseCase(uow.novels, naming)
    result = await use_case.execute(
        conditions,
        order_by=body.order_by,
        order_direction=body.order_direction,
        limit=body.limit,
        min_like=body.min_like,
        min_text=body.min_text,
        format_mode=body.format_mode,
        zip_name=body.zip_name,
        naming_template=body.naming_template,
    )

    from copixiv.domain.services.filename import safe_filename
    desc = safe_filename(result.search_desc.rstrip(".zip").rstrip(".ZIP"))
    headers = {
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(desc + '.zip')}"
        ),
    }
    if result.missing_ids:
        headers["X-Batch-Missing-Ids"] = ",".join(result.missing_ids)

    return StreamingResponse(
        _iter_zip(result.zip_buffer),
        media_type="application/zip",
        headers=headers,
    )


@router.post("/batch-download/preview")
async def batch_download_preview(
    request: Request,
    body: BatchDownloadRequest = Body(...),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = request.app.state.config.batch_download.naming
    use_case = BatchDownloadUseCase(uow.novels, naming)
    path = await use_case.preview(
        conditions,
        order_by=body.order_by,
        order_direction=body.order_direction,
        min_like=body.min_like,
        min_text=body.min_text,
        format_mode=body.format_mode,
        naming_template=body.naming_template,
    )
    return {"path": path}


@router.delete("/{novel_id}", status_code=204)
async def delete_novel(
    request: Request,
    novel_id: int,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    use_case = DeleteNovelUseCase(
        uow.novels, request.app.state.file_storage
    )
    await use_case.execute(novel_id)
