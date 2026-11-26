"""EPUB status reconciliation task adapter (``check_epub``, general lane)."""

from __future__ import annotations

from copixiv.core.models import TaskResult
from copixiv.features.novels.use_cases.epub_reconcile import (
    _STALE_DAYS,
    _SWEEP_CHUNK,
    _SWEEP_READ_PAGE,
    _no_images_ever_and_stale,
    _scan_placeholder,
    _sweep,
    reconcile_epub_status,
)
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


@register("check_epub", description="核对 EPUB 状态与磁盘文件是否一致", lane="general")
async def check_epub(ctx: TaskContext) -> TaskResult:
    """Synchronise ``has_epub`` status with actual files on disk."""
    return await reconcile_epub_status(ctx.require_uow(), source_registry=ctx.source_registry)


__all__ = [
    "_STALE_DAYS",
    "_SWEEP_CHUNK",
    "_SWEEP_READ_PAGE",
    "_no_images_ever_and_stale",
    "_scan_placeholder",
    "_sweep",
    "check_epub",
]
