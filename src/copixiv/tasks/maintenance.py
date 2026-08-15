"""Maintenance tasks — data repair, consistency checks, and index rebuilds.

These are registered task functions that fix up stale or missing data.
They only depend on the database and (optionally) the Pixiv API client.

All tasks return a :class:`TaskResult` with a human-readable summary.
Since these are maintenance tasks (not novel discovery), the
``new_novel_titles`` list is always empty — the notifier will send a
plain summary instead of incorrectly labelling results as "new novels".
"""

from pathlib import Path
import time

from copixiv.domain.models.novel import EpubStatus
from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.services.language import has_image_placeholders
from copixiv.application.author.resolve_names import resolve_author_names
from copixiv.infrastructure.database.write_lock import db_write
from copixiv.app.logger import logger

from .registry import register


@register("check_epub")
async def check_epub(
    *,
    uow,
):
    """Synchronise ``has_epub`` status with actual files on disk.

    * 1 (pending) + file exists        → 2 (completed)
    * 2 (completed) + file gone        → 1 (pending)
    * 1 (pending) + file missing      → 0 (downgraded) when the body text
      no longer contains image placeholders (author removed the images)
    * 1 (pending) + file missing      → 0 (downgraded) when the body still
      has placeholders but no image file was ever downloaded and the last
      attempt is stale (> 7 days) — the images are gone for good
    * 1 (pending) + file missing      → stays pending otherwise
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
    downgrade_ids: list[int] = []
    pending_ids: list[int] = []

    for novel_id, path_str, has_epub_status in rows:
        if path_str:
            txt_path = Path(path_str)
            epub_path = txt_path.with_suffix(".epub")
            if epub_path.exists():
                if has_epub_status == EpubStatus.PENDING:
                    completed_ids.append(novel_id)
            else:
                if has_epub_status == EpubStatus.DONE:
                    revert_ids.append(novel_id)
                elif has_epub_status == EpubStatus.PENDING:
                    if _txt_has_no_images(txt_path):
                        downgrade_ids.append(novel_id)
                    elif _no_images_ever_and_stale(txt_path, novel_id):
                        downgrade_ids.append(novel_id)
                    else:
                        pending_ids.append(novel_id)
        else:
            if has_epub_status == EpubStatus.DONE:
                revert_ids.append(novel_id)
            elif has_epub_status == EpubStatus.PENDING:
                pending_ids.append(novel_id)

    if completed_ids:
        async with db_write():
            async with uow.begin():
                await uow.novels.update_has_epub_status(completed_ids, EpubStatus.DONE)

    if revert_ids:
        async with db_write():
            async with uow.begin():
                await uow.novels.update_has_epub_status(revert_ids, EpubStatus.PENDING)

    if downgrade_ids:
        async with db_write():
            async with uow.begin():
                await uow.novels.update_has_epub_status(downgrade_ids, EpubStatus.NO)

    logger.info(
        f"check_epub: completed={len(completed_ids)}, reverted={len(revert_ids)}, "
        f"downgraded={len(downgrade_ids)}, pending={len(pending_ids)}",
    )

    parts: list[str] = []
    if completed_ids:
        parts.append(f"{len(completed_ids)} 本标记为已完成")
    if revert_ids:
        parts.append(f"{len(revert_ids)} 本回退为待处理")
    if downgrade_ids:
        parts.append(f"{len(downgrade_ids)} 本降级为无图")
    if pending_ids:
        parts.append(f"{len(pending_ids)} 本仍待处理")

    return TaskResult(summary="EPUB 状态检查: " + (" ".join(parts) or "无变化"))


def _txt_has_no_images(txt_path: Path) -> bool:
    """True when the novel text file exists and has no image placeholders.

    A missing/unreadable text file returns False — we cannot judge, so
    the novel stays pending rather than being wrongly downgraded.
    """
    try:
        text = txt_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return not has_image_placeholders(text)


_STALE_DAYS = 7


def _no_images_ever_and_stale(txt_path: Path, novel_id: int) -> bool:
    """True when no image file was ever downloaded and the last attempt is stale.

    The body still has image placeholders, but there is no ``{id}_u_*`` /
    ``{id}_p_*`` file on disk (the download never succeeded) and the txt
    file — whose mtime tracks the last download attempt — is older than
    ``_STALE_DAYS``.  That means the images are gone for good (deleted by
    the author, or the URLs are dead); keeping such novels PENDING forever
    just accumulates zombie rows.

    Freshly-downloaded novels are never downgraded: their mtime is recent,
    so they stay pending and can retry.
    """
    parent = txt_path.parent
    if any(parent.glob(f"{novel_id}_u_*")) or any(parent.glob(f"{novel_id}_p_*")):
        return False
    try:
        age_days = (time.time() - txt_path.stat().st_mtime) / 86400
    except OSError:
        return False
    return age_days > _STALE_DAYS


@register("sync_empty_name")
async def sync_empty_name(
    *,
    client,
    uow,
    write_lock,
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
    resolved = await resolve_author_names(
        author_ids, client=client, uow=uow, write_lock=write_lock,
    )

    # 诚实统计：resolved 才是实际成功解析的作者数；novel 行由
    # update_author_name 按作者批量补齐（rows 全部会被处理）。
    author_count = len(resolved)
    return TaskResult(
        summary=f"作者名同步: 处理 {len(rows)} 本空名小说 ({author_count} 位作者解析成功)"
    )


@register("rebuild_fts")
async def rebuild_fts(
    *,
    uow,
):
    """Rebuild the FTS5 index from scratch.

    Use this after upgrading an existing v1 database whose ``novel_fts``
    rows predate the tags column — a rebuild is required before keyword
    search can hit tag-only text.
    """
    async with db_write():
        async with uow.begin():
            count = await uow.novels.rebuild_fts()

    return TaskResult(summary=f"FTS 索引重建完成（{count} 本小说）")


@register("check_fts")
async def check_fts(
    *,
    uow,
):
    """FTS5 index health check — corruption, orphans, and missing entries.

    Read-only (no write lock).  Reports the index-entry/novel counts and
    any orphan (index row without a novel) / missing (novel without an
    index row) entries, so ops can decide whether a ``rebuild_fts`` run
    is needed.
    """
    from copixiv.infrastructure.repositories.fts import FTSManager

    async with uow.begin():
        result = FTSManager(uow.session).check_fts_health()

    if not result["fts_table_exists"]:
        return TaskResult(summary="FTS 检查: 索引表不存在，需运行 rebuild_fts")

    parts = [f"条目 {result['fts_entry_count']}/{result['novel_count']}"]
    if result.get("orphan_entries"):
        parts.append(f"孤儿 {result['orphan_entries']}")
    if result.get("missing_entries"):
        parts.append(f"缺失 {result['missing_entries']}")
    if result.get("error"):
        parts.append(f"错误 {result['error']}")

    status = "健康" if result["is_healthy"] else "异常"
    return TaskResult(summary=f"FTS 检查({status}): " + ", ".join(parts))


@register("fix_series_index")
async def fix_series_index(
    *,
    client,
    uow,
):
    """Fix novels whose ``series_index`` is NULL by assigning chapter
    numbers from series order (sorted by novel ID ≈ creation time).

    Listing APIs (user_novels, novel_follow, novel_series) do **not**
    include the ``series.index`` field, but ``novel_series`` returns
    all novels in a series in chronological order.  We assign indices
    locally (1, 2, 3, …) and upsert them.

    Each series is fetched and committed immediately so partial
    progress is preserved even if the task times out.
    """
    from copixiv.domain.services.novel_factory import build_from_novel_info
    from copixiv.domain.services.parsing import safe_get, safe_set
    from .pipeline import _batch_upsert

    async with uow.begin():
        series_ids = await uow.series.series_with_empty_index()

    if not series_ids:
        return TaskResult(summary="系列章节号检查: 无需修复")

    total = len(series_ids)
    logger.info(
        f"fix_series_index: {total} series have novels with NULL series_index"
    )

    done = 0
    processed = 0
    for sid in series_ids:
        resp = await client.novel_series(sid, fetch_all=True)
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        # Sort by novel ID (lower ID ≈ earlier chapter), assign indices
        novels.sort(key=lambda n: safe_get(n, "id", 0))
        for i, n in enumerate(novels):
            safe_set(n, "series.index", i + 1)
        novel_models = [build_from_novel_info(n) for n in novels]
        async with db_write():
            async with uow.begin():
                await _batch_upsert(novel_models, uow)
        done += 1
        processed += len(novel_models)

    if done == 0:
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    return TaskResult(
        summary=f"系列章节号修复: {done}/{total} 个系列, 处理 {processed} 本小说"
    )
