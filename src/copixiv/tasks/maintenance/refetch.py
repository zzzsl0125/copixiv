"""``refetch_missing_source`` task adapter (general lane)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from copixiv.core.models import TaskResult
from copixiv.features.novels.use_cases.refetch_missing import (
    refetch_missing_novels,
)
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class RefetchMissingSourceArgs(BaseModel):
    """Parameters for ``refetch_missing_source``."""

    apply: bool = Field(
        default=False,
        description="真正重抓。默认 false = 只统计候选（不发请求、不写库）",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="本轮最多重抓多少本（None = 全部候选）",
    )
    force_recent_days: int = Field(
        default=0,
        ge=0,
        description=("跳过最近 N 天内已经失败过的行（0 = 不跳过）。用于避免网络抖动时同一批行每轮重复烧请求"),
    )


@register(
    "refetch_missing_source",
    description="重抓正文文件已丢失的小说（has_epub=4；默认只统计不抓）",
    args=RefetchMissingSourceArgs,
    lane="general",
)
async def refetch_missing_source(
    args: RefetchMissingSourceArgs,
    ctx: TaskContext,
) -> TaskResult:
    """Re-download novels whose local text file is gone."""
    return await refetch_missing_novels(
        apply=args.apply,
        limit=args.limit,
        force_recent_days=args.force_recent_days,
        uow=ctx.require_uow(),
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        source_service=ctx.source_service,
    )


__all__ = [
    "RefetchMissingSourceArgs",
    "refetch_missing_source",
]
