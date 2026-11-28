"""Novel batch operation and export endpoints."""

from fastapi import APIRouter, BackgroundTasks, Depends

from copixiv.core.exceptions import ValidationError
from copixiv.core.naming import NovelNamingTemplate
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
    BatchExportPreviewRequest,
    BatchExportPreviewResponse,
    BatchExportRequest,
    BatchExportResponse,
    BatchMetadataRequest,
    BatchMetadataResponse,
    BatchOperationRequest,
    BatchOperationResponse,
)
from copixiv.features.novels.use_cases.batch_edit_metadata import (
    batch_edit_metadata,
)
from copixiv.features.novels.use_cases.batch_operations import (
    batch_delete,
    batch_tag,
    delete_novel_paths,
    resolve_batch_scope,
)
from copixiv.storage.zip import zip_entry_path

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/batch", response_model=BatchOperationResponse)
def batch_operation(
    body: BatchOperationRequest,
    background_tasks: BackgroundTasks,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    ids = resolve_batch_scope(body.scope.novel_ids)
    if body.expand_series:
        with uow.begin(read_only=True):
            ids = SQLAlchemyNovelRepository(uow.session).expand_series_ids(ids)
    if body.operation == "delete":
        affected = batch_delete(
            ids,
            uow=uow,
            file_storage=file_storage,
            defer_file_cleanup=lambda paths: background_tasks.add_task(
                delete_novel_paths,
                file_storage,
                paths,
            ),
        )
    else:
        affected = batch_tag(body.operation, ids, body.tags, uow=uow)
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
    ids = resolve_batch_scope(body.scope.novel_ids)
    if body.expand_series:
        ids = await read_transaction(
            uow,
            lambda tx: SQLAlchemyNovelRepository(tx.session).expand_series_ids(ids),
        )

    changes = body.model_dump(exclude_unset=True)
    field_changes = {name: changes[name] for name in ("author_id", "series_id") if name in changes}

    counts = await batch_edit_metadata(
        ids,
        uow=uow,
        client=client,
        apply=body.apply,
        **field_changes,
    )
    return BatchMetadataResponse(**counts)


@router.post("/batch-export/preview", response_model=BatchExportPreviewResponse)
async def batch_export_preview(
    body: BatchExportPreviewRequest,
    app_config: AppConfigDep,
    uow: UowDep,
):
    """Preview the ZIP entry path for the highest-ID selected novel."""
    naming = body.naming_template or app_config.batch_download.naming
    try:
        template = NovelNamingTemplate(naming)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    ids = sorted({int(i) for i in body.novel_ids})
    if not ids:
        return BatchExportPreviewResponse(path=None)

    def _read_preview(tx):
        repo = SQLAlchemyNovelRepository(tx.session)
        latest = repo.get_latest_novel_by_ids(ids)
        if latest is None:
            return None
        if body.merge_series and latest.series_id is not None:
            card = repo.get_folded_series_card(latest.series_id)
            if card is not None:
                return card.model_copy(update={"series_name": None, "series_index": None})
        return latest

    novel = await read_transaction(uow, _read_preview)
    if novel is None:
        return BatchExportPreviewResponse(path=None)
    return BatchExportPreviewResponse(
        path=zip_entry_path(novel, body.format_mode, template),
    )


@router.post("/batch-export", response_model=BatchExportResponse)
def batch_export_task(
    body: BatchExportRequest,
    app_config: AppConfigDep,
    task_manager: TaskManagerDep,
):
    """Enqueue a batch export into the background task system.

    The ZIP is built offline (progress in 「任务管理」) and downloaded via
    ``GET /api/novels/export/{task_id}/download`` — the page can be closed
    while it runs.
    """
    ids = resolve_batch_scope(body.novel_ids, empty_message="请先勾选要导出的小说")
    naming = body.naming_template or app_config.batch_download.naming
    try:
        NovelNamingTemplate(naming)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    task_id = task_manager.run_task(
        "batch_export",
        params={
            "novel_ids": ids,
            "format_mode": body.format_mode.value,
            "zip_name": body.zip_name,
            "naming_template": naming,
            "merge_series": body.merge_series,
        },
    )
    return BatchExportResponse(task_id=task_id, matched=len(ids))
