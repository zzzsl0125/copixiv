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
import uuid
from collections.abc import Sequence
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import TaskResult
from copixiv.core.text import MAX_TAG_LENGTH, validate_tags
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.use_cases.batch_operations import (
    BATCH_ID_CHUNK_SIZE,
    BATCH_MAX_TAGS,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.log import logger
from copixiv.storage.orphan_cleanup import EXPORT_FILE_PREFIX
from copixiv.storage.zip import build_batch_zip
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register

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


async def _execute_batch_chunk(
    ctx: TaskContext,
    operation: str,
    chunk: list[int],
    tag_set: set[str],
    file_storage: Any,
) -> None:
    """Apply one batch chunk inside its own transaction (plus post-commit cleanup)."""
    if operation == "delete":
        paths = await run_write_transaction(
            ctx.uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).delete_many(chunk),
        )
        # File cleanup AFTER the transaction — offload blocking unlinks
        # off the event loop (DB-first, same as the sync path).
        await asyncio.to_thread(_delete_novel_paths, file_storage, paths)
    elif operation == "add_tags":
        await run_write_transaction(
            ctx.uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).add_tags_to_novels(chunk, tag_set),
        )
    else:  # remove_tags
        await run_write_transaction(
            ctx.uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).remove_tags_from_novels(chunk, tag_set),
        )


@register(
    BATCH_TASK_NAME,
    description="对选中小说批量执行删除或标签增删",
    args=BatchOperationArgs,
    lane="general",
)
async def batch_operation(
    args: BatchOperationArgs,
    ctx: TaskContext,
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

    tag_set: set[str] = set()
    if args.operation in ("add_tags", "remove_tags"):
        cleaned = validate_tags(
            list(args.tags or []),
            max_count=BATCH_MAX_TAGS,
            max_length=MAX_TAG_LENGTH,
            require_non_empty=True,
        )
        alias_map = await read_transaction(
            ctx.uow,
            lambda uw: SQLAlchemyTagRepository(uw.session).get_alias_map(),
        )
        tag_set = {alias_map.get(t, t) for t in cleaned}

    chunks = [ids[i : i + BATCH_TASK_SAFETY_CHUNK] for i in range(0, total, BATCH_TASK_SAFETY_CHUNK)]
    done = 0
    failed_chunks = 0

    file_storage = ctx.require_file_storage()
    op_label = _OP_LABELS[args.operation]

    await ctx.report_progress(f"{op_label}进行中：已开始，共 {total} 篇")

    for idx, chunk in enumerate(chunks, start=1):
        try:
            await _execute_batch_chunk(ctx, args.operation, chunk, tag_set, file_storage)

            done += len(chunk)
            await ctx.report_progress(
                f"{op_label}进行中：第 {idx}/{len(chunks)} 批，已处理 {done}/{total} 篇"
            )
        except Exception:  # noqa: BLE001 — one bad chunk must not kill the rest
            failed_chunks += 1
            logger.exception(
                "批量任务 {} 第 {}/{} 批失败",
                op_label,
                idx,
                len(chunks),
            )

    summary = f"{op_label}完成：共处理 {done}/{total} 篇"
    if failed_chunks:
        summary += f"，{failed_chunks} 批失败（详见任务日志，可重新提交）"
    return TaskResult(summary=summary)


def _delete_novel_paths(file_storage, paths: Sequence[str | None]) -> None:
    """Best-effort delete novel files off the event loop."""
    for path in paths:
        if path:
            try:
                file_storage.delete_novel_files(path)
            except Exception:  # noqa: BLE001
                logger.exception("删除小说文件失败: {}", path)


# ---------------------------------------------------------------------------
# Batch export — builds the ZIP in the background so the page can be closed.
# ---------------------------------------------------------------------------

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

    novels: list[dict] = []
    for i in range(0, total, BATCH_ID_CHUNK_SIZE):
        chunk = ids[i : i + BATCH_ID_CHUNK_SIZE]

        def _read_chunk(uw, chunk: list[int] = chunk) -> list:
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

    try:
        try:
            zip_buf, _titles, missing = await asyncio.to_thread(
                build_batch_zip,
                novels,
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

    out_path = export_dir / f"{EXPORT_FILE_PREFIX}{ctx.task_id}.zip"
    size_bytes = await asyncio.to_thread(_write_export_zip, zip_buf, out_path)

    size_mb = size_bytes / 1024 / 1024
    summary = f"批量导出完成：共 {total} 篇（{size_mb:.1f} MB），可在任务管理点击下载"
    if missing:
        summary += f"，缺失文件 {len(missing)} 篇"
    return TaskResult(summary=summary)


def _prepare_export_dir(export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    _sweep_old_exports(export_dir)


def _write_export_zip(zip_buf: BinaryIO, out_path: Path) -> int:
    """Persist *zip_buf* to *out_path* off the event loop and return its size."""
    tmp_path = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        zip_buf.seek(0)
        with open(tmp_path, "wb") as f:
            while data := zip_buf.read(1 << 20):
                f.write(data)
        tmp_path.replace(out_path)
        return out_path.stat().st_size
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        zip_buf.close()


def _sweep_old_exports(export_dir: Path) -> None:
    """Delete export files older than 24h so the download dir never fills up."""
    now = time.time()
    for p in export_dir.glob(f"{EXPORT_FILE_PREFIX}*.zip"):
        try:
            if now - p.stat().st_mtime > _EXPORT_MAX_AGE_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            logger.exception("清理旧导出文件失败: {}", p)
