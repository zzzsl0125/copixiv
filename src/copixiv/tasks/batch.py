"""Background task: batch export (ZIP building).

Registered under ``batch_export``.  Runs inside the task system
(the user can close the page — the export continues).
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import TaskResult
from copixiv.db.tx import read_transaction
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, StoredNovel
from copixiv.features.novels.use_cases.batch_operations import BATCH_ID_CHUNK_SIZE
from copixiv.log import logger
from copixiv.storage.orphan_cleanup import EXPORT_FILE_PREFIX
from copixiv.storage.zip import build_batch_zip_to_file
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register

EXPORT_TASK_NAME = "batch_export"


class BatchExportArgs(BaseModel):
    novel_ids: list[int]
    format_mode: str = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


_EXPORT_MAX_AGE_SECONDS = 24 * 3600


@register(
    EXPORT_TASK_NAME,
    description="把选中小说打包成 ZIP 导出",
    args=BatchExportArgs,
    lane="general",
)
async def batch_export(args: BatchExportArgs, ctx: TaskContext) -> TaskResult:
    """Build the export ZIP as a background task.

    The finished file lands at ``<download_dir>/batch_export_<task_id>.zip``
    and is served by ``GET /api/novels/export/{task_id}/download``.  Old
    export files (>24h) are swept on each run.
    """
    ids = sorted({int(i) for i in args.novel_ids})
    if not ids:
        raise ValidationError("没有可导出的小说")
    total = len(ids)

    # The download endpoint resolves ``batch_export_{task_id}.zip`` from the
    # history row id, so an absent task id cannot produce a reachable file.
    # ``task_id or 0`` used to write ``batch_export_0.zip`` — silently
    # unreachable, and overwritten by the next such run.  Fail loudly instead.
    if ctx.task_id is None:
        raise ValidationError("导出任务缺少 task_id，无法生成可下载的文件")

    export_dir = Path(ctx.require_file_storage().download_dir)
    await asyncio.to_thread(_prepare_export_dir, export_dir)

    await ctx.report_progress("批量导出进行中：收集小说信息…")

    novels: list[StoredNovel] = []
    for i in range(0, total, BATCH_ID_CHUNK_SIZE):
        chunk = ids[i : i + BATCH_ID_CHUNK_SIZE]

        def _read_chunk(uw, chunk: list[int] = chunk) -> list[StoredNovel]:
            return SQLAlchemyNovelRepository(uw.session).get_novels_by_ids(chunk)

        # One read transaction per id chunk (one thread hop each).
        novels.extend(await read_transaction(ctx.uow, _read_chunk))
    if not novels:
        raise ValidationError("所选小说均不存在")

    await ctx.report_progress(f"批量导出进行中：打包中（共 {total} 篇）…")

    # Throttled progress: the zip builder (worker thread) fires every 500
    # files — only write a history update at most every ~3 seconds, and never
    # overlap two progress writes.
    loop = asyncio.get_running_loop()
    last_progress_at = 0.0
    pending_progress: Future[None] | None = None

    def on_progress(processed: int, _total: int) -> None:
        nonlocal last_progress_at, pending_progress
        now = time.monotonic()
        if now - last_progress_at < 3 or ctx.task_id is None:
            return
        if pending_progress is not None and not pending_progress.done():
            return
        last_progress_at = now
        pending_progress = asyncio.run_coroutine_threadsafe(
            ctx.report_progress(f"批量导出进行中：打包中 {processed}/{total} 篇"),
            loop,
        )

    out_path = export_dir / f"{EXPORT_FILE_PREFIX}{ctx.task_id}.zip"
    try:
        try:
            size_bytes, added_count, missing = await asyncio.to_thread(
                build_batch_zip_to_file,
                novels,
                out_path,
                args.format_mode,
                args.naming_template,
                on_progress,
                download_root=export_dir,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    finally:
        if pending_progress is not None:
            with suppress(Exception):
                await asyncio.wrap_future(pending_progress)

    if not added_count:
        raise NotFoundError("未找到可下载的有效文件")

    size_mb = size_bytes / 1024 / 1024
    summary = f"批量导出完成：共 {total} 篇（{size_mb:.1f} MB），可在任务管理点击下载"
    if missing:
        summary += f"，缺失文件 {len(missing)} 篇"
    return TaskResult(summary=summary)


def _prepare_export_dir(export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    _sweep_old_exports(export_dir)


def _sweep_old_exports(export_dir: Path) -> None:
    """Delete export files older than 24h so the download dir never fills up."""
    now = time.time()
    for p in export_dir.glob(f"{EXPORT_FILE_PREFIX}*.zip"):
        try:
            if now - p.stat().st_mtime > _EXPORT_MAX_AGE_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            logger.exception("清理旧导出文件失败: {}", p)
