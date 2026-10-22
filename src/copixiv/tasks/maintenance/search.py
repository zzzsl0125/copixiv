"""Library-wide repairs: empty author names and the keyword-search index.

``sync_empty_name`` / ``rebuild_fts`` / ``check_fts`` — split verbatim out of
``copixiv.tasks.maintenance`` (see that package's docstring).

None of these touch EPUB state or series metadata: they repair the novel
table's author names and the ``pg_bigm`` index respectively.
"""

from copixiv.core.models import TaskResult
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.authors.resolve_names import (
    collect_author_names,
    writeback_author_names,
)

from ..kernel import TaskContext
from ..kernel import register


@register("sync_empty_name", description="补齐作者名为空的小说")
async def sync_empty_name(ctx: TaskContext) -> TaskResult:
    """Fix novels whose ``author_name`` is NULL.

    Collects names via :func:`collect_author_names` (local ``author``
    table first, then Pixiv API) and writes them back in a short write
    transaction via :func:`writeback_author_names`.

    **Disabled by default** — ``scheduled_task.is_enabled`` defaults to
    false, so it only runs once someone enables a schedule for it.  It is
    therefore *not* the guarantee behind "every book shows an author name"
    (LIBRARY_DESIGN.md §3.3): that is a product promise, and what actually
    re-resolves a pending name is a later fetch of the same work / author.
    """
    from sqlalchemy import select as _select
    from copixiv.db import models

    uow = ctx.uow

    async with uow.begin():
        stmt = _select(
            models.Novel.id, models.Novel.author_id
        ).where(models.Novel.author_name.is_(None))
        rows = uow.session.execute(stmt).fetchall()

    if not rows:
        return TaskResult(summary="作者名同步: 无需修复")

    author_ids = {row.author_id for row in rows}
    mapping = await collect_author_names(
        author_ids, client=ctx.client, uow=uow,
    )
    await run_write_transaction(
        uow, lambda uw: writeback_author_names(mapping, uw),
    )

    # 诚实统计：mapping 才是实际成功解析的作者数；novel 行由
    # update_author_name 按作者批量补齐（rows 全部会被处理）。
    author_count = len(mapping)
    return TaskResult(
        summary=f"作者名同步: 处理 {len(rows)} 本空名小说 ({author_count} 位作者解析成功)"
    )



@register("rebuild_fts", description="重建关键词搜索索引")
async def rebuild_fts(ctx: TaskContext) -> TaskResult:
    """Rebuild the keyword-search index (``REINDEX``).

    The index is a ``pg_bigm`` GIN index over the generated column
    ``novel.search_lower`` (migration 0007), maintained by PostgreSQL, so
    there is no derived data to recompute — a rebuild is physical index
    maintenance (bloat, after a bulk load).
    """
    from copixiv.features.novels.search import reindex

    uow = ctx.uow

    async def _reindex(uw) -> None:
        reindex(uw.session)

    await run_write_transaction(uow, _reindex)

    return TaskResult(summary="搜索索引重建完成（REINDEX novel_search_bigm）")


@register("check_fts", description="检查关键词搜索索引是否有效")
async def check_fts(ctx: TaskContext) -> TaskResult:
    """Keyword-search index health check — existence and validity.

    Read-only (no write transaction).  Content drift is impossible by
    construction (``novel.search_lower`` is a generated column, so the index
    is derived from the row), so the check reports whether the index exists
    and is valid, plus the novel count.
    """
    from copixiv.features.novels.search import index_health

    uow = ctx.uow

    async with uow.begin():
        result = index_health(uow.session)

    if not result["index_exists"]:
        return TaskResult(summary="搜索索引检查: 索引不存在，需运行 rebuild_fts")

    status = "健康" if result["is_healthy"] else "异常"
    parts = [f"小说 {result['novel_count']} 本"]
    if not result["is_valid"]:
        parts.append("索引无效（需 rebuild_fts）")
    if result.get("error"):
        parts.append(f"错误 {result['error']}")
    return TaskResult(summary=f"搜索索引检查({status}): " + ", ".join(parts))
