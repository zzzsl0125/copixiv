"""``fix_series_index`` task adapter (general lane)."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from copixiv.core.models import TaskResult
from copixiv.features.series.repair import run_series_repair
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class FixSeriesIndexArgs(BaseModel):
    """Parameters for ``fix_series_index``."""

    max_series: int | None = None
    max_orphans: int | None = None
    max_sweep: int | None = None
    sweep_all: bool = False

    @field_validator("max_series", "max_orphans", "max_sweep", mode="before")
    @classmethod
    def _blank_means_unlimited(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sweep_all", mode="before")
    @classmethod
    def _blank_means_off(cls, value):
        if isinstance(value, str) and not value.strip():
            return False
        return value


@register(
    "fix_series_index",
    description="修复小说章节号与系列归属的数据问题",
    args=FixSeriesIndexArgs,
    lane="general",
)
async def fix_series_index(
    args: FixSeriesIndexArgs,
    ctx: TaskContext,
) -> TaskResult:
    """Repair missing/mismatched ``series_index`` values and orphan rows."""
    return await run_series_repair(
        uow=ctx.require_uow(),
        client=ctx.require_client(),
        max_series=args.max_series,
        max_orphans=args.max_orphans,
        max_sweep=args.max_sweep,
        sweep_all=args.sweep_all,
    )


__all__ = [
    "FixSeriesIndexArgs",
    "fix_series_index",
]
