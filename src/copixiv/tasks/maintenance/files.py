"""``prune_orphan_files`` — reclaim unmanaged files on disk (general lane)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field

from copixiv.core.models import TaskResult
from copixiv.db.tx import read_transaction
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.storage.orphan_cleanup import (
    EXPORT_FILE_PREFIX,
    collect_orphan_files,
    extract_managed_novel_id,
    unlink_files,
)
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class PruneOrphanFilesArgs(BaseModel):
    """Parameters for ``prune_orphan_files``."""

    apply: bool = Field(
        default=False,
        description="真正删除。默认 false = 只统计不删（试运行）",
    )
    min_age_days: int = Field(
        default=7,
        ge=0,
        description=(
            "只处理超过这么多天没改动的文件。默认 7：正在下载的书先写文件、后写行，太小会误删在途作品"
        ),
    )


@register(
    "prune_orphan_files",
    description="清理磁盘上没有任何小说行指向的遗留文件（默认只统计不删）",
    args=PruneOrphanFilesArgs,
    lane="general",
)
async def prune_orphan_files(
    args: PruneOrphanFilesArgs,
    ctx: TaskContext,
) -> TaskResult:
    """Reclaim files left behind by deletions that did not finish."""
    if ctx.file_storage is None:
        return TaskResult(summary="孤儿文件清理: 未配置文件存储，跳过")

    root = Path(ctx.file_storage.download_dir).resolve()
    if not root.is_dir():
        return TaskResult(summary=f"孤儿文件清理: 下载目录不存在 {root}")

    known_ids = await read_transaction(
        ctx.require_uow(),
        lambda uw: SQLAlchemyNovelRepository(uw.session).list_all_ids(),
    )

    now = time.time()
    orphans = await asyncio.to_thread(
        collect_orphan_files,
        root,
        known_ids,
        args.min_age_days,
        now,
    )
    total_bytes = sum(size for _path, size in orphans)
    size_mb = total_bytes / (1024 * 1024)

    if not orphans:
        return TaskResult(summary=(f"孤儿文件清理: 没有可回收的文件（{len(known_ids)} 本书全部有主）"))

    if not args.apply:
        logger.info(
            f"prune_orphan_files: 试运行，可回收 {len(orphans)} 个文件 "
            f"{size_mb:.1f} MB（加 apply=true 才真正删除）"
        )
        return TaskResult(
            summary=(
                f"孤儿文件清理（试运行）: 可回收 {len(orphans)} 个文件、"
                f"{size_mb:.1f} MB；未删除任何文件"
                "（设 apply=true 执行）"
            )
        )

    deleted, freed = await asyncio.to_thread(
        unlink_files,
        [p for p, _size in orphans],
    )
    logger.info(
        f"prune_orphan_files: 删除 {deleted}/{len(orphans)} 个孤儿文件，释放 {freed / (1024 * 1024):.1f} MB"
    )
    return TaskResult(
        summary=(
            f"孤儿文件清理: 删除 {deleted} 个文件、"
            f"释放 {freed / (1024 * 1024):.1f} MB"
            + (f"（{len(orphans) - deleted} 个删除失败，见日志）" if deleted < len(orphans) else "")
        )
    )


__all__ = [
    "EXPORT_FILE_PREFIX",
    "PruneOrphanFilesArgs",
    "collect_orphan_files",
    "extract_managed_novel_id",
    "prune_orphan_files",
    "unlink_files",
]
