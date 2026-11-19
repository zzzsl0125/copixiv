"""Novel batch operation and export endpoints."""

import re
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from copixiv.core.exceptions import ValidationError
from copixiv.core.naming import safe_filename
from copixiv.core.search import parse_search_keyword
from copixiv.core.text import MAX_TAG_LENGTH, validate_tags
from copixiv.db.tx import read_transaction, run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import (
    AppConfigDep,
    FileStorageDep,
    PixivClientDep,
    TaskManagerDep,
    UowDep,
    require_api_key,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import (
    BatchDownloadRequest,
    BatchExportRequest,
    BatchExportResponse,
    BatchMetadataRequest,
    BatchMetadataResponse,
    BatchOperationRequest,
    BatchOperationResponse,
    BatchTaskResponse,
)
from copixiv.features.novels.use_cases.batch_download import BatchDownloadUseCase
from copixiv.features.novels.use_cases.batch_edit_metadata import (
    BatchEditMetadataUseCase,
)
from copixiv.features.novels.use_cases.batch_operations import (
    BATCH_MAX_TAGS,
    BatchDeleteUseCase,
    BatchTagUseCase,
    resolve_batch_scope,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository

router = APIRouter(dependencies=[Depends(require_api_key)])

_ZIP_SUFFIX_RE = re.compile(r"(?i)(?:\.zip)+$")


def _strip_zip_suffix(name: str) -> str:
    """Strip trailing ``.zip`` extensions case-insensitively without stripping characters."""
    return _ZIP_SUFFIX_RE.sub("", name.strip())


def _iter_zip(buffer) -> Iterator[bytes]:
    """Stream a ZIP buffer in chunks, closing it when done."""
    try:
        while chunk := buffer.read(1 << 16):
            yield chunk
    finally:
        buffer.close()


@router.post("/batch-download")
async def batch_download_novels(
    body: BatchDownloadRequest,
    app_config: AppConfigDep,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = app_config.batch_download.naming
    use_case = BatchDownloadUseCase(
        uow,
        naming,
        download_dir=file_storage.download_dir,
    )
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

    base_desc = _strip_zip_suffix(result.search_desc) or "批量下载"
    desc = safe_filename(base_desc)
    headers = {
        "Content-Disposition": (f"attachment; filename*=UTF-8''{quote(desc + '.zip')}"),
    }
    if result.missing_ids:
        headers["X-Batch-Missing-Ids"] = ",".join(result.missing_ids)

    return StreamingResponse(
        _iter_zip(result.zip_buffer),
        media_type="application/zip",
        headers=headers,
    )


@router.post("/batch-download/preview")
def batch_download_preview(
    body: BatchDownloadRequest,
    app_config: AppConfigDep,
    uow: UowDep,
):
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    naming = app_config.batch_download.naming
    use_case = BatchDownloadUseCase(uow, naming)
    path = use_case.preview(
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
def batch_operation(
    body: BatchOperationRequest,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    with uow.begin():
        ids = resolve_batch_scope(
            SQLAlchemyNovelRepository(uow.session),
            mode=body.scope.mode,
            novel_ids=body.scope.novel_ids,
            keyword=body.scope.keyword,
            min_like=body.scope.min_like,
            min_text=body.scope.min_text,
            excluded_ids=body.scope.excluded_ids,
        )
    if body.operation == "delete":
        use_case = BatchDeleteUseCase(uow, file_storage)
        affected = use_case.execute(ids)
    else:

        def _tag_tx(tx: SqlUnitOfWork) -> int:
            use_case = BatchTagUseCase(
                SQLAlchemyNovelRepository(tx.session),
                SQLAlchemyTagRepository(tx.session),
            )
            return use_case.execute(body.operation, ids, body.tags)

        affected = run_write_transaction_sync(uow, _tag_tx)
    return BatchOperationResponse(matched=len(ids), affected=affected)


@router.post("/batch-task", response_model=BatchTaskResponse)
def batch_task_operation(
    body: BatchOperationRequest,
    task_manager: TaskManagerDep,
    uow: UowDep,
):
    """Enqueue a batch operation into the background task system.

    Unlike :func:`batch_operation` (synchronous, capped at
    :data:`BATCH_MAX_NOVELS`), this path accepts selections of ANY size —
    the task chunks the work, so the user can close the page and watch
    progress in 「任务管理」.  Validation (empty scope / tags) still
    happens synchronously so mistakes surface immediately.
    """
    scope = body.scope
    cleaned_tags: list[str] = list(body.tags)
    if body.operation in ("add_tags", "remove_tags"):
        cleaned_tags = validate_tags(
            list(body.tags),
            max_count=BATCH_MAX_TAGS,
            max_length=MAX_TAG_LENGTH,
            require_non_empty=True,
        )

    with uow.begin():
        ids = resolve_batch_scope(
            SQLAlchemyNovelRepository(uow.session),
            mode=scope.mode,
            novel_ids=scope.novel_ids,
            keyword=scope.keyword,
            min_like=scope.min_like,
            min_text=scope.min_text,
            excluded_ids=scope.excluded_ids or [],
            cap=None,
        )

    task_id = task_manager.run_task(
        "batch_operation",
        params={
            "operation": body.operation,
            "novel_ids": ids,
            "tags": cleaned_tags,
        },
    )
    return BatchTaskResponse(task_id=task_id, matched=len(ids))


@router.post("/batch-metadata", response_model=BatchMetadataResponse)
async def batch_metadata_operation(
    body: BatchMetadataRequest,
    uow: UowDep,
    client: PixivClientDep,
):
    """批量编辑本地书的「作者」/「系列」—— 预演（``apply=false``，默认）或落库。

    与单本 ``PATCH /{novel_id}`` 同一套三态与收尾语义（名字由 id 解析、解析失败
    落 NULL 待补、解除关联时作者留名 / 系列连名字与章节号一起清），唯一刻意的
    差异是**来源**：pixiv 书不报 400，而是被跳过并计数
    （``applicable`` / ``skipped``）——批量的选择集天然混合，整体 400 会让这个
    功能不可用。论证与写路径纪律见 ``batch_edit_metadata.py``。

    ``apply=false`` 是**预演**：只算数、一个字都不写（不建占位 author / series 行、
    不解析名字、不打网络），用户先看到「N 篇本地书将被修改、M 篇 pixiv 书跳过」
    再确认；``apply=true`` 先算数再落库，``affected`` 是真正被改动的行数。

    ``cap=None``：与 ``batch_task_operation`` 同一口径，不引入新的 5000 上限——
    写操作是**一条** set-based UPDATE（``update_metadata_many``），天然快，
    ``matched`` 可以任意大。作用域解析仍是唯一那条规则（``resolve_batch_scope``）；
    来源收窄（只动本地书）由用例做，全量 id 原样交给它。

    路由是**精确路径** ``/batch-metadata``，与 ``/batch`` / ``/batch-export``
    同形：``POST /{novel_id}/...`` 那几条都要第二段路径，所以不会把
    ``batch-metadata`` 当成 ``novel_id``（``batch-export`` 早已是同样的形状）。
    """
    ids = await read_transaction(
        uow,
        lambda tx: resolve_batch_scope(
            SQLAlchemyNovelRepository(tx.session),
            mode=body.scope.mode,
            novel_ids=body.scope.novel_ids,
            keyword=body.scope.keyword,
            min_like=body.scope.min_like,
            min_text=body.scope.min_text,
            excluded_ids=body.scope.excluded_ids,
            cap=None,
        ),
    )

    changes = body.model_dump(exclude_unset=True)
    field_changes = {name: changes[name] for name in ("author_id", "series_id") if name in changes}

    use_case = BatchEditMetadataUseCase(uow, client=client)
    counts = await use_case.execute(ids, apply=body.apply, **field_changes)
    return BatchMetadataResponse(**counts)


@router.post("/batch-export", response_model=BatchExportResponse)
def batch_export_task(
    body: BatchExportRequest,
    task_manager: TaskManagerDep,
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
