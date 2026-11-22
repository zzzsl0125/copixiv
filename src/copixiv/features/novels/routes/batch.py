"""Novel batch operation and export endpoints."""

import re
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse

from copixiv.core.exceptions import ValidationError
from copixiv.core.naming import safe_filename
from copixiv.core.search import parse_search_keyword
from copixiv.db.tx import read_transaction
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
)
from copixiv.features.novels.use_cases.batch_download import BatchDownloadUseCase
from copixiv.features.novels.use_cases.batch_edit_metadata import (
    BatchEditMetadataUseCase,
)
from copixiv.features.novels.use_cases.batch_operations import (
    BatchDeleteUseCase,
    BatchTagUseCase,
    delete_novel_paths,
    resolve_batch_scope,
)

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
    )
    return {"path": path}


@router.post("/batch", response_model=BatchOperationResponse)
def batch_operation(
    body: BatchOperationRequest,
    background_tasks: BackgroundTasks,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    with uow.begin(read_only=True):
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
        affected = use_case.execute(
            ids,
            defer_file_cleanup=lambda paths: background_tasks.add_task(
                delete_novel_paths,
                file_storage,
                paths,
            ),
        )
    else:
        affected = BatchTagUseCase(uow).execute(body.operation, ids, body.tags)
    return BatchOperationResponse(matched=len(ids), affected=affected)


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
