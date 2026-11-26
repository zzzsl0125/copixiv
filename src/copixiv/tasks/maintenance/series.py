"""``fix_series_index`` task adapter (general lane)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from copixiv.core.models import SOURCE_PIXIV, TaskResult
from copixiv.features.series.service import run_series_repair
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class FixSeriesIndexArgs(BaseModel):
    """Parameters for ``fix_series_index``."""

    model_config = ConfigDict(extra="ignore")

    max_series: int | None = 200

    @field_validator("max_series", mode="before")
    @classmethod
    def _blank_means_unlimited(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@register(
    "fix_series_index",
    description="修复缺失或错误的小说章节号",
    args=FixSeriesIndexArgs,
    lane="general",
    notify=False,
)
async def fix_series_index(
    args: FixSeriesIndexArgs,
    ctx: TaskContext,
) -> TaskResult:
    """Repair missing/mismatched ``series_index`` values using Pixiv series rosters."""
    if ctx.source_service is not None:
        ctx.source_service.require_enabled(SOURCE_PIXIV)
    return await run_series_repair(
        uow=ctx.require_uow(),
        client=ctx.require_client(),
        max_series=args.max_series,
    )


__all__ = [
    "FixSeriesIndexArgs",
    "fix_series_index",
]
