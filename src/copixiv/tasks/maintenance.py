"""Maintenance tasks — data repair, consistency checks, and index rebuilds.

Registered task functions that fix up stale or missing data.  They only
depend on the database and (optionally) the Pixiv API client, all reached
through the :class:`TaskContext` (docs/MODULARITY.md §M8).

All tasks return a :class:`TaskResult` with a human-readable summary.
Since these are maintenance tasks (not novel discovery), the
``new_novel_titles`` list is always empty — the notifier will send a
plain summary instead of incorrectly labelling results as "new novels".
"""

from pathlib import Path
import asyncio
import time

from copixiv.features.authors.resolve_names import collect_author_names, writeback_author_names
from copixiv.core.models import EpubStatus
from copixiv.core.models import TaskResult
from copixiv.core.services import has_image_placeholders
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.novels.repo import (
    SQLAlchemyNovelRepository,
    SQLAlchemySeriesRepository,
)
from copixiv.storage.epub.builder import is_valid_epub, resolve_epub_path
from copixiv.log import logger

from .kernel import TaskContext
from .kernel import register


@register("check_epub")
async def check_epub(ctx: TaskContext) -> TaskResult:
    """Synchronise ``has_epub`` status with actual files on disk.

    Every row is examined; ``0`` (unclassified, the legacy column default)
    is classified into a real state instead of being skipped, which is what
    made 3 118 demoted/legacy rows unreachable for five weeks:

    * 1 (pending)   + valid file exists → 2 (completed)
    * 1 (pending)   + valid file exists but the EPUB still contains raw
      image placeholders → stays 1 (queued for regeneration)
    * 1 (pending)   + valid file exists but embedded no image while the body
      has placeholders → 1 (queued for regeneration)
    * 2 (completed) + file gone         → 1 (pending)  [revert]
    * 2 (completed) + file corrupt (not a zip) → 1 (pending) [revert]
    * 0 (unclassified) + valid file + body has placeholders + the EPUB
      actually embedded images → 2 (completed).  Heals the novels whose
      EPUB was written but never marked, without trusting a file that was
      written while its images were still missing.
    * 0 (unclassified) + file missing + body has image placeholders
      → 1 (pending): queue it for (re)generation.
    * 1 or 0 + file missing + body has NO placeholders → 3 (no images).
      This is the terminal "nothing to build" state.  It replaces the old
      ``→ 0`` demotion, which reused the same value as "not yet classified"
      and was therefore both invisible to this task and indistinguishable
      from work still to do.
    * any + file missing + placeholders but no image file was ever
      downloaded and the last attempt is stale (> 7 days) → 3 (no images) —
      the author removed the images, there is nothing left to embed.
    * 1 (pending)   + file missing → stays pending otherwise

    Cost: a ``3`` row with no file is skipped without touching the disk, and
    a ``2`` row is skipped as soon as the file is a valid zip — so the
    238 936-row sweep only re-reads text for rows that can still change
    state, and ``0`` disappears after the first full pass.

    The EPUB-content scans (:func:`_epub_has_placeholders`,
    :func:`_epub_has_images`) only run for rows that need them.
    """
    from sqlalchemy import select as _select
    from copixiv.db import models

    uow = ctx.uow

    async with uow.begin():
        stmt = _select(
            models.Novel.id, models.Novel.path, models.Novel.has_epub
        )
        rows = uow.session.execute(stmt).fetchall()

    if not rows:
        return TaskResult(summary="EPUB 状态检查: 无需修复")

    # The sweep is file-I/O bound over every novel (238 936 rows measured in
    # production) and takes minutes.  It must not monopolise the event loop:
    # on 2026-09-14 a single run left every API endpoint timing out while it
    # held the single worker thread — the dispatcher itself stalled behind
    # it.  So the scan is chunked and offloaded to a *shared* thread pool,
    # yielding to the event loop between chunks so HTTP requests keep being
    # served while the sweep crawls through the disk.
    completed_ids: list[int] = []
    revert_ids: list[int] = []
    no_image_ids: list[int] = []
    enable_ids: list[int] = []
    pending_ids: list[int] = []

    for start in range(0, len(rows), _SWEEP_CHUNK):
        chunk = rows[start:start + _SWEEP_CHUNK]
        (
            c_done, c_revert, c_noimg, c_enable, c_pending,
        ) = await asyncio.to_thread(_sweep, chunk, start)
        completed_ids += c_done
        revert_ids += c_revert
        no_image_ids += c_noimg
        enable_ids += c_enable
        pending_ids += c_pending
        # Explicit yield: give the HTTP handlers a turn between chunks.
        await asyncio.sleep(0)

    async def _apply(ids: list[int], status: EpubStatus) -> None:
        await run_write_transaction(
            uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).update_has_epub_status(
                ids, status
            ),
        )

    if completed_ids:
        await _apply(completed_ids, EpubStatus.DONE)

    if revert_ids:
        await _apply(revert_ids, EpubStatus.PENDING)

    if enable_ids:
        await _apply(enable_ids, EpubStatus.PENDING)

    if no_image_ids:
        await _apply(no_image_ids, EpubStatus.NO_IMAGES)

    logger.info(
        f"check_epub: completed={len(completed_ids)}, reverted={len(revert_ids)}, "
        f"enabled={len(enable_ids)}, no_images={len(no_image_ids)}, "
        f"pending={len(pending_ids)}",
    )

    parts: list[str] = []
    if completed_ids:
        parts.append(f"{len(completed_ids)} 本标记为已完成")
    if revert_ids:
        parts.append(f"{len(revert_ids)} 本回退为待处理")
    if enable_ids:
        parts.append(f"{len(enable_ids)} 本启用为待处理")
    if no_image_ids:
        parts.append(f"{len(no_image_ids)} 本确认为无图")
    if pending_ids:
        parts.append(f"{len(pending_ids)} 本仍待处理")

    return TaskResult(summary="EPUB 状态检查: " + (" ".join(parts) or "无变化"))




def _sweep(
    rows: list, offset: int = 0,
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    """Classify *rows* against the files on disk (runs in a worker thread).

    Pure file I/O + decisions — no database access, so it can be offloaded
    with :func:`asyncio.to_thread` without touching the session.  *offset*
    is the index of the first row within the whole table, used only so the
    progress log reports absolute positions.  Returns
    ``(completed, reverted, no_images, enabled, pending)`` id lists.
    """
    completed_ids: list[int] = []
    revert_ids: list[int] = []
    no_image_ids: list[int] = []
    enable_ids: list[int] = []
    pending_ids: list[int] = []

    for row_no, (novel_id, path_str, status) in enumerate(rows, 1):
        if (offset + row_no) % 50000 == 0:
            logger.info(f"check_epub: 已检查 {offset + row_no} 行")

        if not path_str:
            if status == EpubStatus.DONE:
                revert_ids.append(novel_id)
            elif status == EpubStatus.PENDING:
                pending_ids.append(novel_id)
            continue

        txt_path = Path(path_str)
        epub_path = resolve_epub_path(txt_path)
        epub_ok = is_valid_epub(epub_path)

        if status == EpubStatus.DONE:
            # A file that is not a valid zip is as good as missing — two
            # such novels were served to readers for months.
            if not epub_ok:
                revert_ids.append(novel_id)
            continue

        # Body scan — but a terminal row only needs it when a *readable* file
        # appeared (the cheap zip check decides that first), and a DONE row
        # was already handled above.  Keeps the steady-state sweep free of
        # text I/O for the ~227 k terminal rows.
        if status == EpubStatus.NO_IMAGES and not epub_ok:
            continue

        # The body (canonical source of truth) is read once per row.
        # ``None`` means "cannot judge" — missing or unreadable text must
        # never be turned into a terminal state.
        canonical = _txt_has_images(txt_path)

        if status == EpubStatus.NO_IMAGES:
            # Terminal "nothing to build" — but a complete file may have
            # appeared since (a rebuild, or a re-download that carried no
            # placeholder).  A complete EPUB is the *only* promotion
            # evidence; a bare zip is not (that is how shell/partial files
            # got blessed as done before).
            if epub_ok and _epub_is_complete(epub_path):
                completed_ids.append(novel_id)
            continue

        if status == EpubStatus.PENDING:
            if not epub_ok:
                if canonical is False:
                    no_image_ids.append(novel_id)
                elif _no_images_ever_and_stale(txt_path, novel_id):
                    no_image_ids.append(novel_id)
                else:
                    pending_ids.append(novel_id)
            elif canonical is False:
                # A file exists but the body needs nothing embedded.
                no_image_ids.append(novel_id)
            elif canonical is None:
                pending_ids.append(novel_id)   # unreadable → do not judge
            elif _epub_is_complete(epub_path):
                completed_ids.append(novel_id)
            else:
                # Written while its images were missing: keep it queued so a
                # later sweep rebuilds it instead of advertising it as done.
                pending_ids.append(novel_id)
            continue

        # status == NO (unclassified, or any unexpected value).
        if canonical is False:
            no_image_ids.append(novel_id)
        elif canonical is None:
            continue          # unreadable text → stay unclassified
        elif not epub_ok or not _epub_is_complete(epub_path):
            # Missing file, or a file written before its images landed
            # (nothing embedded) → queue it for proper (re)generation.
            enable_ids.append(novel_id)
        else:
            completed_ids.append(novel_id)

    return completed_ids, revert_ids, no_image_ids, enable_ids, pending_ids


def _epub_is_complete(epub_path: Path) -> bool:
    """True when *epub_path* is a usable book: no raw marker left, ≥1 image.

    This is the single "the produced EPUB is actually finished" predicate.
    Both the ``→ 2`` promotions (from ``1`` and from ``0``) use it, so a file
    written while its images were missing can never be blessed as done no
    matter which status the row happens to carry — the asymmetry that let a
    leftover-marker EPUB be re-promoted from ``0`` to ``2`` and then never
    re-examined.
    """
    return _epub_has_images(epub_path) and not _epub_has_placeholders(epub_path)


def _txt_has_images(txt_path: Path) -> bool | None:
    """``True`` / ``False`` for the body's placeholder state, ``None`` if unknown.

    The tri-state matters: a *missing* or unreadable text file must never be
    read as "no placeholders", because that would write the terminal
    ``NO_IMAGES`` state on a novel that may well need an EPUB (one permission
    or I/O hiccup would hide it forever).
    """
    return _scan_placeholder(txt_path)


def _scan_placeholder(txt_path: Path) -> bool | None:
    """Scan *txt_path* for an image placeholder without decoding it.

    Returns True/False for "has placeholders"/"has none", or None when the
    file cannot be read (missing/unreadable → the caller must not judge).

    Chunked byte search: this runs over every row of the production table
    (238 936 rows in the 2026-09 sweep), so it must not decode whole novels
    into Python strings just to look for a marker.  ``overlap`` keeps a
    marker that straddles a chunk boundary detectable.
    """
    needles = (b"[uploadedimage:", b"[pixivimage:")
    overlap = max(len(n) for n in needles) - 1
    try:
        with open(txt_path, "rb") as fh:
            tail = b""
            while chunk := fh.read(1 << 20):
                buf = tail + chunk
                if any(n in buf for n in needles):
                    return True
                tail = buf[-overlap:]
    except OSError:
        return None
    return False


_STALE_DAYS = 7

# Rows classified per worker-thread hop.  Small enough that the event loop
# gets a turn (and HTTP keeps being served) several times a second, large
# enough that the `to_thread` overhead stays negligible.
_SWEEP_CHUNK = 4000


def _epub_text(epub_path: Path) -> bytes | None:
    """Concatenated XHTML members of *epub_path*, or None when unreadable."""
    import zipfile

    try:
        with zipfile.ZipFile(epub_path) as zf:
            return b"".join(
                zf.read(name)
                for name in zf.namelist()
                if name.endswith((".xhtml", ".html", ".htm"))
            )
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _epub_has_placeholders(epub_path: Path) -> bool:
    """True when the built EPUB still contains raw image placeholders.

    Those novels were written while their images were missing: the marker
    survived as literal text (216 of them in the 2026-09 audit), so the
    EPUB is not actually finished and must not read as completed.
    """
    text = _epub_text(epub_path)
    if text is None:
        return False
    return has_image_placeholders(text.decode("utf-8", errors="ignore"))


def _epub_has_images(epub_path: Path) -> bool:
    """True when the EPUB embedded at least one illustration.

    Guards the promotion to ``2``: a file written from a draft whose images
    had not landed yet is empty of pictures and must be regenerated instead
    of being blessed as done.

    Deliberately only counts the ``images/`` folder — the cover lives at
    ``EPUB/cover.jpg`` and must not satisfy this check, or every cover-only
    EPUB would pass as having its illustrations embedded.
    """
    import zipfile

    try:
        with zipfile.ZipFile(epub_path) as zf:
            # A real file entry, not the "images/" directory entry itself —
            # an empty EPUB can carry the directory and nothing in it.
            return any(
                "images/" in info.filename and not info.is_dir()
                for info in zf.infolist()
            )
    except (OSError, zipfile.BadZipFile):
        return False


def _no_images_ever_and_stale(txt_path: Path, novel_id: int) -> bool:
    """True when no image file was ever downloaded and the last attempt is stale.

    The body still has image placeholders, but there is no ``{id}_u_*`` /
    ``{id}_p_*`` file on disk (the download never succeeded) and the txt
    file — whose mtime tracks the last download attempt — is older than
    ``_STALE_DAYS``.  That means the images are gone for good (deleted by
    the author, or the URLs are dead); keeping such novels PENDING forever
    just accumulates zombie rows.  The row goes to the terminal
    ``NO_IMAGES`` state — never to the unclassified ``NO``, which used to
    swallow rows into a state the reconciler could not even see.

    Freshly-downloaded novels are never abandoned: their mtime is recent,
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
async def sync_empty_name(ctx: TaskContext) -> TaskResult:
    """Fix novels whose ``author_name`` is NULL.

    Collects names via :func:`collect_author_names` (local ``author``
    table first, then Pixiv API) and writes them back in a short write
    transaction via :func:`writeback_author_names`.
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


@register("rebuild_fts")
async def rebuild_fts(ctx: TaskContext) -> TaskResult:
    """Rebuild the keyword-search index (``REINDEX``).

    The index is an expression index over ``novel`` maintained by PostgreSQL
    (migration 0003), so there is no derived data to recompute — a rebuild is
    physical index maintenance (bloat, after a bulk load).
    """
    from copixiv.features.novels.search import reindex

    uow = ctx.uow

    async def _reindex(uw) -> None:
        reindex(uw.session)

    await run_write_transaction(uow, _reindex)

    return TaskResult(summary="搜索索引重建完成（REINDEX novel_search_gin）")


@register("check_fts")
async def check_fts(ctx: TaskContext) -> TaskResult:
    """Keyword-search index health check — existence and validity.

    Read-only (no write transaction).  Content drift is impossible by
    construction (the index is derived from the ``novel`` row), so the check
    reports whether the index exists and is valid, plus the novel count.
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


@register("fix_series_index")
async def fix_series_index(ctx: TaskContext) -> TaskResult:
    """Fix novels whose ``series_index`` is NULL by assigning chapter
    numbers from series order (sorted by novel ID ≈ creation time).

    Listing APIs (user_novels, novel_follow, novel_series) do **not**
    include the ``series.index`` field, but ``novel_series`` returns
    all novels in a series in chronological order.  We assign indices
    locally (1, 2, 3, …) and upsert them.

    Each series is fetched and committed immediately so partial
    progress is preserved even if the task times out.
    """
    from copixiv.core.draft import build_from_novel_info
    from copixiv.core.services import safe_get, safe_set
    from copixiv.features.novels.ingest import batch_upsert

    uow = ctx.uow

    async with uow.begin():
        series_ids = await SQLAlchemySeriesRepository(uow.session).series_with_empty_index()

    if not series_ids:
        return TaskResult(summary="系列章节号检查: 无需修复")

    total = len(series_ids)
    logger.info(
        f"fix_series_index: {total} series have novels with NULL series_index"
    )

    done = 0
    processed = 0
    for sid in series_ids:
        resp = await ctx.client.novel_series(sid, fetch_all=True)
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        # Sort by novel ID (lower ID ≈ earlier chapter), assign indices
        novels.sort(key=lambda n: safe_get(n, "id", 0))
        for i, n in enumerate(novels):
            safe_set(n, "series.index", i + 1)
        novel_models = [build_from_novel_info(n) for n in novels]
        await run_write_transaction(
            uow, lambda uw: batch_upsert(novel_models, uw),
        )
        done += 1
        processed += len(novel_models)

    if done == 0:
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    return TaskResult(
        summary=f"系列章节号修复: {done}/{total} 个系列, 处理 {processed} 本小说"
    )


@register("rebuild_tag_counts")
async def rebuild_tag_counts(ctx: TaskContext) -> TaskResult:
    """Recompute every tag's ``reference_count`` from ``novel.tags``.

    The denormalized counter drifts over time: v1 legacy data, deletes
    that predated the decrement-on-delete fix, and alias retroactive
    moves all leave stale values.  Measured on the production 232k DB,
    ~10 % of tags (7500/73037) had a count that didn't match the actual
    distinct-novel count, with errors up to ±14000.

    This task recalculates ``reference_count`` from the array column that
    is now the single source of truth
    (``SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name]``) in a
    single correlated UPDATE — fast, exact, and independent of the
    trigger's incremental bookkeeping.
    """
    from sqlalchemy import text, func, select
    from copixiv.db import models

    uow = ctx.uow

    async def _run(uow):
        uow.session.execute(text(
            "UPDATE tag SET reference_count = ("
            "  SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name]"
            ")"
        ))
        # SQLAlchemy doesn't report rowcount reliably for correlated
        # UPDATEs, so count tags explicitly.
        total = uow.session.execute(
            select(func.count()).select_from(models.Tag)
        ).scalar() or 0
        drifted = uow.session.execute(text(
            "SELECT COUNT(*) FROM tag WHERE reference_count != ("
            "  SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name])"
        )).scalar() or 0
        return total, drifted

    total, drifted = await run_write_transaction(uow, _run)

    # The commit above (uow.begin() exit) bumps the data epoch, so the
    # count cache invalidates itself — no manual invalidate needed.

    return TaskResult(
        summary=f"标签引用计数重建: {total} 个标签, 修正 {drifted} 个偏差"
    )
