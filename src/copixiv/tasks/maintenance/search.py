"""Library-wide metadata repair task adapter (``sync_empty_name``, general lane)."""

from copixiv.core.models import TaskResult
from copixiv.features.authors.service import sync_empty_author_names
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


@register("sync_empty_name", description="补齐作者名为空的小说", lane="general")
async def sync_empty_name(ctx: TaskContext) -> TaskResult:
    """Fix novels whose ``author_name`` is NULL."""
    return await sync_empty_author_names(
        uow=ctx.require_uow(),
        client=ctx.client,
    )
