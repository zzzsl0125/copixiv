"""Maintenance tasks — data repair, consistency checks, and index rebuilds.

These are registered task functions that fix up stale or missing data.
They only depend on the database and (optionally) the Pixiv API client.

All tasks return a :class:`TaskResult` with a human-readable summary.
Since these are maintenance tasks (not novel discovery), the
``new_novel_titles`` list is always empty — the notifier will send a
plain summary instead of incorrectly labelling results as "new novels".
"""

from pathlib import Path

from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.services.author_name_resolver import resolve_author_names
from copixiv.app.logger import logger

from .registry import register


@register("check_epub")
async def check_epub(
    *,
    uow,
):
    """Synchronise ``has_epub`` status with actual files on disk.

    * 1 (pending) + file exists  → 2 (completed)
    * 2 (completed) + file gone  → 1 (pending)
    * 1 (pending) + file missing → stays pending (needs download)
    """
    from sqlalchemy import select as _select
    from copixiv.infrastructure.database import models

    async with uow.begin():
        stmt = _select(
            models.Novel.id, models.Novel.path, models.Novel.has_epub
        ).where(models.Novel.has_epub > 0)
        rows = uow.session.execute(stmt).fetchall()

    if not rows:
        return TaskResult(summary="EPUB 状态检查: 无需修复")

    completed_ids: list[int] = []
    revert_ids: list[int] = []
    pending_ids: list[int] = []

    for novel_id, path_str, has_epub_status in rows:
        if path_str:
            epub_path = Path(path_str).with_suffix(".epub")
            if epub_path.exists():
                if has_epub_status == 1:
                    completed_ids.append(novel_id)
            else:
                if has_epub_status == 2:
                    revert_ids.append(novel_id)
                elif has_epub_status == 1:
                    pending_ids.append(novel_id)
        else:
            if has_epub_status == 2:
                revert_ids.append(novel_id)
            elif has_epub_status == 1:
                pending_ids.append(novel_id)

    if completed_ids:
        async with uow.begin():
            await uow.novels.update_has_epub_status(completed_ids, 2)

    if revert_ids:
        async with uow.begin():
            await uow.novels.update_has_epub_status(revert_ids, 1)

    logger.info(
        f"check_epub: completed={len(completed_ids)}, "
        f"reverted={len(revert_ids)}, pending={len(pending_ids)}",
    )

    parts: list[str] = []
    if completed_ids:
        parts.append(f"{len(completed_ids)} 本标记为已完成")
    if revert_ids:
        parts.append(f"{len(revert_ids)} 本回退为待处理")
    if pending_ids:
        parts.append(f"{len(pending_ids)} 本仍待处理")

    return TaskResult(summary="EPUB 状态检查: " + ("; ".join(parts) or "无变化"))


@register("sync_empty_name")
async def sync_empty_name(
    *,
    client,
    uow,
):
    """Fix novels whose ``author_name`` is NULL.

    Delegates to :func:`resolve_author_names` which checks the local
    ``author`` table first, then falls back to the Pixiv API.
    """
    from sqlalchemy import select as _select
    from copixiv.infrastructure.database import models

    async with uow.begin():
        stmt = _select(
            models.Novel.id, models.Novel.author_id
        ).where(models.Novel.author_name.is_(None))
        rows = uow.session.execute(stmt).fetchall()

    if not rows:
        return TaskResult(summary="作者名同步: 无需修复")

    author_ids = {row.author_id for row in rows}
    resolved = await resolve_author_names(author_ids, client=client, uow=uow)

    total_fixed = len(rows)
    author_count = len(resolved)
    return TaskResult(
        summary=f"作者名同步: 修复了 {total_fixed} 本小说 ({author_count} 位作者)"
    )


@register("rebuild_fts")
async def rebuild_fts(
    *,
    uow,
):
    """Rebuild the FTS5 index."""
    async with uow.begin():
        await uow.novels.rebuild_fts()

    return TaskResult(summary="FTS 索引重建完成")
