"""Novel API endpoints."""

from collections.abc import Iterator
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Body, Request
from fastapi.responses import FileResponse, StreamingResponse

from copixiv.deps import (
    get_app_config, get_file_storage, get_session_factory,
    get_task_manager, get_uow, get_write_uow, parse_json_param,
)
from copixiv.core.services import parse_search_keyword
from copixiv.core.services import QuerySpec
from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.features.novels.schemas import (
    BatchDownloadRequest,
    BatchExportRequest,
    BatchExportResponse,
    BatchOperationRequest,
    BatchOperationResponse,
    BatchTaskResponse,
    MatchIdsRequest,
    MatchIdsResponse,
    NovelIdsResponse,
    NovelListResponse,
    NovelsByIdsRequest,
    NovelsByIdsResponse,
    SortIdsRequest,
)
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db import constants as C
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from .record_history import record_search_history
from copixiv.features.novels.batch_download import BatchDownloadUseCase
from copixiv.features.novels.batch_operations import (
    BATCH_ID_CHUNK_SIZE,
    BATCH_MAX_NOVELS,
    BATCH_MAX_TAGS,
    BatchDeleteUseCase,
    BatchTagUseCase,
    resolve_batch_scope,
)
from copixiv.features.novels.delete_novel import DeleteNovelUseCase
from copixiv.features.novels.get_novel_file import GetNovelFileUseCase

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
    background_tasks: BackgroundTasks,
    session_factory=Depends(get_session_factory),
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    order_by: str = C.ORDER_BY_RANDOM,
    order_direction: str = "DESC",
    cursor: str | None = None,
    per_page: int = Query(20, ge=1, le=200),
    min_like: int | None = None,
    min_text: int | None = None,
    exclude_blocked: bool | None = Query(None),
):
    conditions = parse_search_keyword(keyword) if keyword else None
    cursor_dict = parse_json_param(cursor, "cursor")

    spec = QuerySpec(
        conditions=conditions or [],
        order_by=order_by,
        order_direction=order_direction,
        cursor=cursor_dict,
        per_page=per_page,
        min_like=min_like,
        min_text=min_text,
        exclude_blocked_tags=exclude_blocked,
    )
    results = await SQLAlchemyNovelRepository(uow.session).get_novels(spec)

    # 首屏搜索响应附带「范围内是否存在被厌恶标签排除的小说」——镜像谓词
    # 存在性查询（~ms 级），让 ExclusionBar 只在确有被隐藏小说时显示。
    # load-more（带 cursor）与无关键词浏览不计算，保持默认 False。
    if conditions and cursor_dict is None:
        results["has_excluded"] = await SQLAlchemyNovelRepository(
            uow.session
        ).has_excluded_novels(spec)

    if conditions and background_tasks:
        background_tasks.add_task(
            record_search_history, conditions,
            lambda: SqlUnitOfWork(session_factory),
        )

    return results


@router.get("/count")
async def count_novels(
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
    excluded_ids: list[int] | None = Query(None),
    exclude_blocked: bool | None = Query(None),
    with_excluded: bool = Query(False),
):
    conditions = parse_search_keyword(keyword) if keyword else None
    spec = QuerySpec(
        conditions=conditions or [],
        min_like=min_like,
        min_text=min_text,
        exclude_ids=excluded_ids or [],
        exclude_blocked_tags=exclude_blocked,
    )
    total = await SQLAlchemyNovelRepository(uow.session).count_novels(spec)
    # How many matching novels were hidden by blocked tags (0 when the
    # exclusion is off / nothing blocked) — powers the ExclusionBar.
    # Off by default: it is the *slow* tail of this endpoint (the same
    # keyword bitmap scan repeats), and since the ExclusionBar switched to
    # a lazy "查看被隐藏的小说" action (which fetches /blocked-ids), no
    # caller needs it eagerly.  Pass ``with_excluded=true`` to opt in.
    excluded = 0
    if with_excluded and exclude_blocked is not False:
        excluded = await SQLAlchemyNovelRepository(uow.session).count_excluded_novels(spec)
    return {"total": total, "excluded": excluded}


@router.get("/ids", response_model=NovelIdsResponse)
async def get_matching_novel_ids(
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
    exclude_blocked: bool | None = Query(None),
):
    """All VISIBLE IDs matching the filters — powers the 「全选匹配」bulk-add action.

    No size cap: the selection itself may be any size (operations beyond
    the sync cap run as background tasks).  ``truncated`` is kept for wire
    compatibility and is always false here.
    """
    conditions = parse_search_keyword(keyword) if keyword else None
    spec = QuerySpec(
        conditions=conditions or [],
        min_like=min_like,
        min_text=min_text,
        exclude_blocked_tags=exclude_blocked,
    )
    total = await SQLAlchemyNovelRepository(uow.session).count_novels(spec)
    ids = await SQLAlchemyNovelRepository(uow.session).list_matching_ids(spec)
    return NovelIdsResponse(ids=ids, total=total, truncated=False)


@router.get("/blocked-ids", response_model=NovelIdsResponse)
async def get_blocked_novel_ids(
    uow: SqlUnitOfWork = Depends(get_uow),
    keyword: str | None = Query(None),
    min_like: int | None = Query(None),
    min_text: int | None = Query(None),
    order_by: str | None = Query(None),
    order_direction: str = Query("DESC"),
):
    """IDs of novels carrying blocked tags WITHIN the current search scope.

    Powers the 「查看被排除」view.  Follows the global exclusion setting:
    empty when exclusion is off.  The blocked-id list is filtered through
    the same scope logic as ``filter_ids_in_scope`` (chunked to stay under
    SQLite's variable limit); exclusion itself is not re-applied (the ids
    are already the excluded set).

    ``order_by`` (id/like/text) orders the result set; ``random`` or
    unsupported values return the scope order.
    """
    conditions = parse_search_keyword(keyword) if keyword else None
    blocked_ids = await SQLAlchemyNovelRepository(uow.session).list_blocked_ids()
    if not blocked_ids:
        return NovelIdsResponse(ids=[], total=0, truncated=False)

    matching: list[int] = []
    for i in range(0, len(blocked_ids), BATCH_ID_CHUNK_SIZE):
        chunk = blocked_ids[i:i + BATCH_ID_CHUNK_SIZE]
        scope_spec = QuerySpec(
            conditions=conditions or [],
            min_like=min_like,
            min_text=min_text,
            exclude_blocked_tags=False,
        )
        matching.extend(
            await SQLAlchemyNovelRepository(uow.session).filter_ids_in_scope(chunk, scope_spec)
        )

    if order_by and order_by != "random":
        matching = await SQLAlchemyNovelRepository(uow.session).sort_novel_ids(
            matching, order_by, order_direction,
        )
    return NovelIdsResponse(
        ids=matching, total=len(matching), truncated=False,
    )


@router.post("/sort-ids", response_model=NovelIdsResponse)
async def sort_novel_ids(
    body: SortIdsRequest = Body(...),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """Order an explicit id list by a novel column — 「查看已选」排序.

    Returns the same ids ordered by id/like/text (missing ids dropped).
    """
    ids = sorted({int(i) for i in body.novel_ids})
    if not ids:
        return NovelIdsResponse(ids=[], total=0, truncated=False)
    ordered = await SQLAlchemyNovelRepository(uow.session).sort_novel_ids(
        ids, body.order_by, body.order_direction,
    )
    return NovelIdsResponse(ids=ordered, total=len(ordered), truncated=False)


@router.post("/by-ids", response_model=NovelsByIdsResponse)
async def get_novels_by_ids(
    body: NovelsByIdsRequest = Body(...),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """Novel details for an explicit ID list — powers the 「查看已选」view.

    Preserves the caller's ID order; missing IDs are dropped.  The input
    is capped at :data:`BATCH_MAX_NOVELS` (the view's page size — the
    frontend pages large selections in 5000-novel slices).
    """
    ids = body.novel_ids[:BATCH_MAX_NOVELS]
    truncated = len(body.novel_ids) > BATCH_MAX_NOVELS
    novels = await SQLAlchemyNovelRepository(uow.session).get_novels_by_ids(ids)
    return NovelsByIdsResponse(novels=novels, truncated=truncated)


@router.post("/match-ids", response_model=MatchIdsResponse)
async def match_novel_ids(
    body: MatchIdsRequest = Body(...),
    uow: SqlUnitOfWork = Depends(get_uow),
    exclude_blocked: bool | None = Query(None),
):
    """Subset of *novel_ids* that match the filters — scoped 「清除选择」.

    Intersects the accumulated selection with the CURRENT search scope so
    the clear action only removes what belongs to the visible scope (the
    rest of the selection survives across searches).  Any-size input: the
    ID list is processed in internal chunks to stay under SQLite's
    variable limit.
    """
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    matching: list[int] = []
    for i in range(0, len(body.novel_ids), BATCH_ID_CHUNK_SIZE):
        chunk = body.novel_ids[i:i + BATCH_ID_CHUNK_SIZE]
        scope_spec = QuerySpec(
            conditions=conditions or [],
            min_like=body.min_like,
            min_text=body.min_text,
            exclude_blocked_tags=exclude_blocked,
        )
        matching.extend(
            await SQLAlchemyNovelRepository(uow.session).filter_ids_in_scope(chunk, scope_spec)
        )
    return MatchIdsResponse(matching_ids=matching, truncated=False)


@router.post("/batch-task", response_model=BatchTaskResponse)
async def batch_task_operation(
    body: BatchOperationRequest = Body(...),
    task_manager=Depends(get_task_manager),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """Enqueue a batch operation into the background task system.

    Unlike :func:`batch_operation` (synchronous, capped at
    :data:`BATCH_MAX_NOVELS`), this path accepts selections of ANY size —
    the task chunks the work, so the user can close the page and watch
    progress in 「任务管理」.  Validation (empty scope / tags) still
    happens synchronously so mistakes surface immediately.
    """
    scope = body.scope
    # Scope resolution lives in the application layer (single rule for the
    # sync and background paths — docs/MODULARITY.md §M7): cap=None because
    # the background task chunks selections of any size.
    ids = await resolve_batch_scope(
        SQLAlchemyNovelRepository(uow.session),
        mode=scope.mode,
        novel_ids=scope.novel_ids,
        keyword=scope.keyword,
        min_like=scope.min_like,
        min_text=scope.min_text,
        excluded_ids=scope.excluded_ids or [],
        cap=None,
    )

    if body.operation in ("add_tags", "remove_tags"):
        raw = {t.strip() for t in body.tags if t and t.strip()}
        if not raw:
            raise ValidationError("请至少输入一个标签")
        if len(raw) > BATCH_MAX_TAGS:
            raise ValidationError(
                f"一次最多操作 {BATCH_MAX_TAGS} 个标签（当前 {len(raw)} 个）"
            )

    task_id = task_manager.run_task(
        "batch_operation",
        params={
            "operation": body.operation,
            "novel_ids": ids,
            "tags": body.tags,
        },
    )
    return BatchTaskResponse(task_id=task_id, matched=len(ids))


@router.post("/{novel_id}/favourite", status_code=204)
async def toggle_favourite(novel_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    await SQLAlchemyNovelRepository(uow.session).toggle_favourite(novel_id)


@router.post("/author/{author_id}/follow", status_code=204)
async def toggle_special_follow(author_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    await SQLAlchemyNovelRepository(uow.session).toggle_special_follow(author_id)


@router.get("/{novel_id}/download")
async def download_novel(
    novel_id: int,
    file_storage=Depends(get_file_storage),
    uow: SqlUnitOfWork = Depends(get_uow),
    format: Literal["txt", "epub"] = "txt",
):
    use_case = GetNovelFileUseCase(
        SQLAlchemyNovelRepository(uow.session), file_storage.download_dir,
    )
    file_path, media_type = await use_case.execute(novel_id, format)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_path.name)}"
    }
    return FileResponse(path=str(file_path), media_type=media_type, headers=headers)


@router.post("/batch-download")
async def batch_download_novels(
    body: BatchDownloadRequest = Body(...),
    app_config=Depends(get_app_config),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = app_config.batch_download.naming
    use_case = BatchDownloadUseCase(SQLAlchemyNovelRepository(uow.session), naming)
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
        novel_ids=body.novel_ids,
        excluded_ids=body.excluded_ids,
    )

    from copixiv.core.services import safe_filename
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
    body: BatchDownloadRequest = Body(...),
    app_config=Depends(get_app_config),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = app_config.batch_download.naming
    use_case = BatchDownloadUseCase(SQLAlchemyNovelRepository(uow.session), naming)
    path = await use_case.preview(
        conditions,
        order_by=body.order_by,
        order_direction=body.order_direction,
        min_like=body.min_like,
        min_text=body.min_text,
        format_mode=body.format_mode,
        naming_template=body.naming_template,
        novel_ids=body.novel_ids,
        excluded_ids=body.excluded_ids,
    )
    return {"path": path}


@router.post("/batch", response_model=BatchOperationResponse)
async def batch_operation(
    body: BatchOperationRequest = Body(...),
    file_storage=Depends(get_file_storage),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    ids = await resolve_batch_scope(
        SQLAlchemyNovelRepository(uow.session),
        mode=body.scope.mode,
        novel_ids=body.scope.novel_ids,
        keyword=body.scope.keyword,
        min_like=body.scope.min_like,
        min_text=body.scope.min_text,
        excluded_ids=body.scope.excluded_ids,
    )
    if body.operation == "delete":
        use_case = BatchDeleteUseCase(SQLAlchemyNovelRepository(uow.session), file_storage)
        affected = await use_case.execute(ids)
    else:
        use_case = BatchTagUseCase(
            SQLAlchemyNovelRepository(uow.session),
            SQLAlchemyTagRepository(uow.session),
        )
        affected = await use_case.execute(body.operation, ids, body.tags)
    return BatchOperationResponse(matched=len(ids), affected=affected)


@router.delete("/{novel_id}", status_code=204)
async def delete_novel(
    novel_id: int,
    file_storage=Depends(get_file_storage),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    use_case = DeleteNovelUseCase(SQLAlchemyNovelRepository(uow.session), file_storage)
    await use_case.execute(novel_id)


@router.post("/batch-export", response_model=BatchExportResponse)
async def batch_export_task(
    body: BatchExportRequest = Body(...),
    task_manager=Depends(get_task_manager),
):
    """Enqueue a batch export into the background task system.

    The ZIP is built offline (progress in 「任务管理」) and downloaded via
    ``GET /api/novels/export/{task_id}/download`` — the page can be closed
    while it runs.
    """
    ids = sorted({int(i) for i in body.novel_ids})
    if not ids:
        raise ValidationError("请先勾选要导出的小说")

    task_id = task_manager.run_task(
        "batch_export",
        params={
            "novel_ids": ids,
            "format_mode": body.format_mode,
            "zip_name": body.zip_name,
            "naming_template": body.naming_template,
        },
    )
    return BatchExportResponse(task_id=task_id, matched=len(ids))


@router.get("/export/{task_id}/download")
async def download_export_file(
    task_id: int,
    file_storage=Depends(get_file_storage),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """Stream a completed background export ZIP to the browser."""
    import json
    from pathlib import Path

    from copixiv.core.services import safe_filename

    file_path = (
        Path(file_storage.download_dir)
        / f"batch_export_{task_id}.zip"
    )
    if not file_path.is_file():
        raise NotFoundError("导出文件不存在（可能已被自动清理），请重新导出")

    # Prefer the user's zip_name (stored in the task arguments).
    filename = f"batch_export_{task_id}.zip"
    arguments = await SQLAlchemyTaskRepository(uow.session).get_task_arguments(task_id)
    if arguments:
        try:
            args = json.loads(arguments)
            zip_name = (args.get("zip_name") or "").strip()
            if zip_name:
                filename = safe_filename(zip_name.rstrip(".zip").rstrip(".ZIP")) + ".zip"
        except (json.JSONDecodeError, TypeError):
            pass

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    return FileResponse(
        path=str(file_path), media_type="application/zip", headers=headers,
    )
