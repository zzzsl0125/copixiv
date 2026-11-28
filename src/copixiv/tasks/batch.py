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
from typing import Any

from pydantic import BaseModel

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import SOURCE_PIXIV, EpubStatus, TaskResult
from copixiv.db.tx import read_transaction
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, StoredNovel
from copixiv.features.novels.use_cases.batch_operations import BATCH_ID_CHUNK_SIZE
from copixiv.log import logger
from copixiv.storage.epub.series_builder import SeriesBookSpec, SeriesChapterSource
from copixiv.storage.orphan_cleanup import EXPORT_FILE_PREFIX
from copixiv.storage.zip import SeriesExportEntry, build_batch_zip_to_file
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register

EXPORT_TASK_NAME = "batch_export"


class BatchExportArgs(BaseModel):
    novel_ids: list[int]
    format_mode: str = "txt"
    zip_name: str | None = None
    naming_template: str | None = None
    merge_series: bool = False


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

    export_items: list[StoredNovel | SeriesExportEntry] = []
    if args.merge_series:
        seen_series: set[int] = set()
        ordered_series_ids: list[int] = []
        for novel in novels:
            if novel.series_id is None:
                export_items.append(novel)
            elif novel.series_id not in seen_series:
                seen_series.add(novel.series_id)
                ordered_series_ids.append(novel.series_id)

        for sid in ordered_series_ids:

            def _read_series(uw, sid: int = sid):
                return SQLAlchemyNovelRepository(uw.session).get_series_book_bundle(sid)

            series_meta, chapters = await read_transaction(ctx.uow, _read_series)
            if not chapters:
                continue
            export_items.append(_build_series_export_entry(sid, series_meta, chapters))
        export_items = [*export_items]  # merged books already populated above
        total = len(export_items)
    else:
        export_items = list(novels)

    if not export_items:
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
                export_items,
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
    count_label = f"{added_count}/{total}" if added_count < total else str(total)
    summary = f"批量导出完成：共 {count_label} 篇（{size_mb:.1f} MB），可在任务管理点击下载"
    if missing:
        summary += f"，缺失文件 {len(missing)} 篇"
    return TaskResult(summary=summary)


def _build_series_export_entry(
    series_id: int,
    series_meta: dict[str, Any] | None,
    chapters: list[StoredNovel],
) -> SeriesExportEntry:
    rep = max(chapters, key=lambda ch: (abs(ch.id), ch.id))
    book_title = (
        ((series_meta or {}).get("series_name") or "").strip()
        or next((ch.series_name for ch in chapters if ch.series_name), None)
        or rep.title
        or f"系列 #{series_id}"
    )
    author_id = (
        (series_meta or {}).get("author_id")
        if (series_meta or {}).get("author_id") is not None
        else (
            rep.author_id
            if rep.author_id is not None
            else next((ch.author_id for ch in chapters if ch.author_id is not None), None)
        )
    )
    author_name = (
        ((series_meta or {}).get("author_name") or "").strip()
        or rep.author_name
        or next((ch.author_name for ch in chapters if ch.author_name), None)
    )
    source = (series_meta or {}).get("source") or str(rep.source or SOURCE_PIXIV)
    total_text = sum(max(0, ch.text) for ch in chapters)
    max_like = max(ch.like for ch in chapters)
    max_view = max(ch.view for ch in chapters)
    statuses = [int(ch.has_epub) for ch in chapters]
    if EpubStatus.DONE in statuses:
        agg_has_epub = EpubStatus.DONE
    elif all(s == EpubStatus.MISSING_SOURCE for s in statuses):
        agg_has_epub = EpubStatus.MISSING_SOURCE
    elif EpubStatus.PENDING in statuses:
        agg_has_epub = EpubStatus.PENDING
    elif all(s == EpubStatus.NO_IMAGES for s in statuses):
        agg_has_epub = EpubStatus.NO_IMAGES
    else:
        agg_has_epub = EpubStatus.NO
    create_times = [ch.create_time for ch in chapters if ch.create_time is not None]
    max_ct = max(create_times) if create_times else None
    chapter_sources = [
        SeriesChapterSource(
            novel_id=ch.id,
            title=ch.title,
            series_index=ch.series_index,
            text_path=Path(ch.path),
            has_epub=int(ch.has_epub),
        )
        for ch in chapters
        if ch.path
    ]
    spec = SeriesBookSpec(
        series_id=series_id,
        title=book_title,
        author_name=author_name,
        source=source,
        chapters=chapter_sources,
    )
    return SeriesExportEntry(
        spec=spec,
        id=rep.id,
        title=book_title,
        author_id=author_id,
        author_name=author_name,
        like=max_like,
        view=max_view,
        text=total_text,
        create_time=max_ct,
        has_epub=agg_has_epub,
        source=source,
    )


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
