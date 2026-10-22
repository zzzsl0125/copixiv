"""Background tasks: batch operations (delete / add_tags / remove_tags)
and batch export (ZIP building).

Registered under ``batch_operation`` / ``batch_export``.  Runs inside the
task system (the user can close the page — the operation continues).

Chunking policy — the original numbers came from the SQLite era and no longer
apply (PostgreSQL has no ``MAX_VARIABLE_NUMBER``), so this is now purely a
**safety/observability** choice, not a hard limit:

- A single whole-library delete of 232,531 rows completed in ~60 s in ONE
  transaction on the old SQLite database.  That measurement is why the design
  used to argue against chunking.
- Under PostgreSQL, one statement addressing 200 k+ ids is legal but holds a
  single very long transaction and reports no intermediate progress.  Chunking
  at :data:`BATCH_TASK_SAFETY_CHUNK` keeps each transaction short, lets the
  task write live progress between chunks, and means one bad chunk cannot
  discard the whole run (each chunk is retried independently by
  ``run_write_transaction``).
- The trade-off is per-chunk fixed overhead; at 30 k rows per chunk that is
  negligible next to the row work.

Tasks self-report progress into their own ``task_history`` row (``task_id``
travels through the TaskContext), so the 「任务管理」page shows live
progress while they run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel

from copixiv.features.novels.batch_operations import (
    BATCH_ID_CHUNK_SIZE,
    BATCH_MAX_TAGS,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from copixiv.core.exceptions import ValidationError
from copixiv.core.models import TaskResult
from copixiv.core.services import build_batch_zip
from copixiv.db.write_lock import run_write_transaction
from copixiv.log import logger

from .kernel import TaskContext
from .kernel import register

BATCH_TASK_NAME = "batch_operation"
EXPORT_TASK_NAME = "batch_export"

# Chunk size for a background batch operation.  Not a database limit any more
# (PostgreSQL has none for ``IN`` lists) — see the module docstring: it bounds
# transaction length, keeps progress reporting alive, and contains failures.
BATCH_TASK_SAFETY_CHUNK = BATCH_ID_CHUNK_SIZE

_OP_LABELS = {
    "delete": "批量删除",
    "add_tags": "批量添加标签",
    "remove_tags": "批量移除标签",
}


# ---------------------------------------------------------------------------
# Argument models (JSON contract per task)
# ---------------------------------------------------------------------------


class BatchOperationArgs(BaseModel):
    operation: str
    novel_ids: list[int]
    tags: list[str] | None = None


class BatchExportArgs(BaseModel):
    novel_ids: list[int]
    format_mode: str = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


@register(
    BATCH_TASK_NAME,
    description="对选中小说批量执行删除或标签增删",
    args=BatchOperationArgs,
)
async def batch_operation(
    args: BatchOperationArgs, ctx: TaskContext,
) -> TaskResult:
    """Execute a batch operation over an explicit ID list.

    Args:
        operation: ``delete`` | ``add_tags`` | ``remove_tags``.
        novel_ids: The full selection (any size).
        tags: Tag names for the tag operations (aliases resolved here).
    """
    if args.operation not in _OP_LABELS:
        raise ValidationError(f"未知的批量操作: {args.operation}")

    ids = sorted({int(i) for i in args.novel_ids})
    if not ids:
        raise ValidationError("没有可处理的小说")
    total = len(ids)

    # ---- tags: normalize + resolve aliases (same rules as the sync path)
    tag_set: set[str] | None = None
    if args.operation in ("add_tags", "remove_tags"):
        raw = {t.strip() for t in (args.tags or []) if t and t.strip()}
        if not raw:
            raise ValidationError("请至少输入一个标签")
        if len(raw) > BATCH_MAX_TAGS:
            raise ValidationError(f"一次最多操作 {BATCH_MAX_TAGS} 个标签")
        async with ctx.uow.begin():
            alias_map = await SQLAlchemyTagRepository(ctx.uow.session).get_alias_map()
        tag_set = {alias_map.get(t, t) for t in raw}

    chunks = [
        ids[i:i + BATCH_TASK_SAFETY_CHUNK]
        for i in range(0, total, BATCH_TASK_SAFETY_CHUNK)
    ]
    done = 0
    failed_chunks = 0

    async def report_progress(stage: str) -> None:
        """Write live progress into the task-history row (best-effort).

        ``status=None``: a progress write must never change the row's state.
        The executor owns ``running`` → terminal; a late progress write that
        carried ``"running"`` would resurrect a finished row (the call below
        is fire-and-forget, so it can land after the final update).
        """
        if ctx.task_id is None:
            return
        try:
            await run_write_transaction(
                ctx.uow,
                lambda uw: SQLAlchemyTaskRepository(uw.session).update_task(
                    ctx.task_id,
                    None,
                    progress=f"{_OP_LABELS[args.operation]}进行中：{stage}",
                ),
            )
        except Exception:  # noqa: BLE001 — progress must never kill the task
            logger.exception("批量任务进度更新失败（不影响执行）")

    await report_progress(f"已开始，共 {total} 篇")

    for idx, chunk in enumerate(chunks, start=1):
        try:
            if args.operation == "delete":
                paths = await run_write_transaction(
                    ctx.uow,
                    lambda uw: SQLAlchemyNovelRepository(uw.session).delete_many(chunk),
                )
                # File cleanup AFTER the transaction — unlink failures must
                # not roll the chunk back (DB-first, same as the sync path).
                for path in paths:
                    if path:
                        try:
                            ctx.file_storage.delete_novel_files(path)
                        except Exception:  # noqa: BLE001
                            logger.exception("删除小说文件失败: %s", path)
            elif args.operation == "add_tags":
                await run_write_transaction(
                    ctx.uow,
                    lambda uw: SQLAlchemyNovelRepository(uw.session).add_tags_to_novels(
                        chunk, tag_set,
                    ),
                )
            else:  # remove_tags
                await run_write_transaction(
                    ctx.uow,
                    lambda uw: SQLAlchemyNovelRepository(uw.session).remove_tags_from_novels(
                        chunk, tag_set,
                    ),
                )

            done += len(chunk)
            await report_progress(
                f"第 {idx}/{len(chunks)} 批，已处理 {done}/{total} 篇"
            )
        except Exception:  # noqa: BLE001 — one bad chunk must not kill the rest
            failed_chunks += 1
            logger.exception(
                "批量任务 %s 第 %d/%d 批失败",
                _OP_LABELS[args.operation], idx, len(chunks),
            )

    summary = f"{_OP_LABELS[args.operation]}完成：共处理 {done}/{total} 篇"
    if failed_chunks:
        summary += f"，{failed_chunks} 批失败（详见任务日志，可重新提交）"
    return TaskResult(summary=summary)


# ---------------------------------------------------------------------------
# Batch export — builds the ZIP in the background so the page can be closed.
# ---------------------------------------------------------------------------

EXPORT_FILE_PREFIX = "batch_export_"
_EXPORT_MAX_AGE_SECONDS = 24 * 3600


@register(
    EXPORT_TASK_NAME,
    description="把选中小说打包成 ZIP 导出",
    args=BatchExportArgs,
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

    export_dir = Path(ctx.file_storage.download_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    _sweep_old_exports(export_dir)

    async def report_progress(stage: str) -> None:
        """See the batch_operation counterpart: progress-only, no status."""
        if ctx.task_id is None:
            return
        try:
            await run_write_transaction(
                ctx.uow,
                lambda uw: SQLAlchemyTaskRepository(uw.session).update_task(
                    ctx.task_id,
                    None,
                    progress=f"批量导出进行中：{stage}",
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("导出任务进度更新失败（不影响执行）")

    await report_progress("收集小说信息…")

    novels: list[dict] = []
    for i in range(0, total, BATCH_ID_CHUNK_SIZE):
        async with ctx.uow.begin():
            chunk = ids[i:i + BATCH_ID_CHUNK_SIZE]
            novels.extend(await SQLAlchemyNovelRepository(ctx.uow.session).get_novels_by_ids(chunk))
    if not novels:
        raise ValidationError("所选小说均不存在")

    await report_progress(f"打包中（共 {total} 篇）…")

    # Throttled progress: the zip builder (worker thread) fires every 500
    # files — only write a history update at most every ~3 seconds.
    loop = asyncio.get_running_loop()
    last_progress_at = 0.0

    def on_progress(processed: int, _total: int) -> None:
        nonlocal last_progress_at
        now = time.monotonic()
        if now - last_progress_at < 3 or ctx.task_id is None:
            return
        last_progress_at = now
        asyncio.run_coroutine_threadsafe(
            report_progress(f"打包中 {processed}/{total} 篇"), loop,
        )

    try:
        zip_buf, _titles, missing = await asyncio.to_thread(
            build_batch_zip,
            novels, args.format_mode, args.naming_template, on_progress,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    out_path = export_dir / f"{EXPORT_FILE_PREFIX}{ctx.task_id}.zip"
    zip_buf.seek(0)
    with open(out_path, "wb") as f:
        while chunk := zip_buf.read(1 << 20):
            f.write(chunk)
    zip_buf.close()

    size_mb = out_path.stat().st_size / 1024 / 1024
    summary = (
        f"批量导出完成：共 {total} 篇（{size_mb:.1f} MB），"
        f"可在任务管理点击下载"
    )
    if missing:
        summary += f"，缺失文件 {len(missing)} 篇"
    return TaskResult(summary=summary)


def _sweep_old_exports(export_dir: Path) -> None:
    """Delete export files older than 24h so the download dir never fills up."""
    now = time.time()
    for p in export_dir.glob(f"{EXPORT_FILE_PREFIX}*.zip"):
        try:
            if now - p.stat().st_mtime > _EXPORT_MAX_AGE_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            logger.exception("清理旧导出文件失败: %s", p)
