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

from pydantic import BaseModel, field_validator

from copixiv.features.authors.resolve_names import collect_author_names, writeback_author_names
from copixiv.core import formats
from copixiv.core.models import EpubStatus
from copixiv.core.models import TaskResult
from copixiv.core.services import has_image_placeholders
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.novels.repo import (
    SQLAlchemyNovelRepository,
    SQLAlchemySeriesRepository,
)
from copixiv.storage.epub.builder import is_valid_epub
from copixiv.log import logger

from .kernel import TaskContext
from .kernel import register


@register("check_epub", description="核对 EPUB 状态与磁盘文件是否一致")
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

    **Bounded per run, resumable.**  The first full pass over production
    measured ~10.5 ms/row — 42 minutes for 238 k rows, well past the task
    runner's 30-minute ``TIMEOUT_SECONDS``, and the previous "scan everything,
    then write everything" shape meant a killed run wrote *nothing* (1374 on
    2026-09-16 died at row 100 k having never applied a single update).  Each
    run now covers at most :data:`_MAX_ROWS_PER_RUN` rows and **applies every
    chunk before advancing a cursor**, so a timeout simply resumes where it
    stopped.  Once the whole table is classified, the cursor resets and later
    sweeps are cheap (terminal rows cost a stat, not a read).
    """
    from sqlalchemy import select as _select
    from copixiv.db import models

    uow = ctx.uow
    started = time.monotonic()
    cursor = await _read_cursor(uow)

    async with uow.begin():
        rows = uow.session.execute(
            _select(models.Novel.id, models.Novel.path, models.Novel.has_epub)
            .where(models.Novel.id > cursor)
            .order_by(models.Novel.id)
            .limit(_MAX_ROWS_PER_RUN + 1)
        ).fetchall()

    if not rows:
        if cursor:
            await _write_cursor(uow, 0)
            return TaskResult(summary="EPUB 状态检查: 全表巡检完成，下轮从头复查")
        return TaskResult(summary="EPUB 状态检查: 无需修复")

    truncated = len(rows) > _MAX_ROWS_PER_RUN
    rows = rows[:_MAX_ROWS_PER_RUN]

    # The sweep is file-I/O bound and must not monopolise the event loop: on
    # 2026-09-14 a single run left every API endpoint timing out while it held
    # the single worker thread.  So the scan is chunked and offloaded to a
    # *shared* thread pool, yielding to the event loop between chunks so HTTP
    # requests keep being served while the sweep crawls through the disk.
    completed_ids: list[int] = []
    revert_ids: list[int] = []
    no_image_ids: list[int] = []
    enable_ids: list[int] = []
    pending_ids: list[int] = []
    totals = {"completed": 0, "reverted": 0, "enabled": 0, "no_images": 0}

    async def _apply(ids: list[int], status: EpubStatus) -> None:
        await run_write_transaction(
            uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).update_has_epub_status(
                ids, status
            ),
        )

    async def _flush() -> None:
        """Persist this chunk's verdicts before moving on.

        Order matters: write the statuses *before* the cursor, so a crash can
        only re-scan rows (idempotent), never skip them.
        """
        if completed_ids:
            await _apply(completed_ids, EpubStatus.DONE)
            totals["completed"] += len(completed_ids)
            completed_ids.clear()
        if revert_ids:
            await _apply(revert_ids, EpubStatus.PENDING)
            totals["reverted"] += len(revert_ids)
            revert_ids.clear()
        if enable_ids:
            await _apply(enable_ids, EpubStatus.PENDING)
            totals["enabled"] += len(enable_ids)
            enable_ids.clear()
        if no_image_ids:
            await _apply(no_image_ids, EpubStatus.NO_IMAGES)
            totals["no_images"] += len(no_image_ids)
            no_image_ids.clear()

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
        await _flush()
        await _write_cursor(uow, int(chunk[-1][0]))
        # Explicit yield: give the HTTP handlers a turn between chunks.
        await asyncio.sleep(0)

    next_cursor = 0 if not truncated else await _read_cursor(uow)
    logger.info(
        f"check_epub: 本轮 {len(rows)} 行（{time.monotonic() - started:.0f}s），"
        f"completed={totals['completed']}, reverted={totals['reverted']}, "
        f"enabled={totals['enabled']}, no_images={totals['no_images']}, "
        f"pending={len(pending_ids)}，游标 {cursor} → {next_cursor}"
        f"{'，下轮继续' if truncated else '，全表完成'}"
    )

    parts: list[str] = []
    if totals["completed"]:
        parts.append(f"{totals['completed']} 本标记为已完成")
    if totals["reverted"]:
        parts.append(f"{totals['reverted']} 本回退为待处理")
    if totals["enabled"]:
        parts.append(f"{totals['enabled']} 本启用为待处理")
    if totals["no_images"]:
        parts.append(f"{totals['no_images']} 本确认为无图")
    if pending_ids:
        parts.append(f"{len(pending_ids)} 本仍待处理")
    tail = (
        f"（本轮 {len(rows)} 行，下轮从 #{next_cursor} 继续）"
        if truncated else f"（全表完成，本轮 {len(rows)} 行）"
    )
    return TaskResult(
        summary="EPUB 状态检查: " + (" ".join(parts) or "无变化") + " " + tail
    )


_CURSOR_KEY = "check_epub.cursor"

# Rows classified per run.  Bounded on purpose: the measured cost is ~10.5 ms
# per unclassified row (a full text read to look for image markers), so 60 k
# rows ≈ 10.5 minutes worst case — a third of the 30-minute task timeout,
# which leaves room for a slow disk.  The cursor makes the split harmless:
# each run is a work slice, not a do-or-die full pass.
#
# After the one-off classification of the legacy ``0`` backlog the per-row cost
# collapses (terminal rows cost a stat, not a read), so this cap is no longer
# binding for routine sweeps — a full pass then finishes in a minute or two.
_MAX_ROWS_PER_RUN = 60_000


async def _read_cursor(uow, key: str = _CURSOR_KEY) -> int:
    """Where the last sweep stopped (0 on the first pass or after wrap-around).

    *key* is the ``setting`` row that holds this task's own resume point:
    every bounded, resumable task must pass its own key — sharing
    ``_CURSOR_KEY`` across tasks would let one task's progress overwrite
    another's.
    """
    import json

    from sqlalchemy import select as _select
    from copixiv.db import models

    async with uow.begin():
        value = uow.session.execute(
            _select(models.Setting.value).where(
                models.Setting.key == key
            )
        ).scalar()
    if not value:
        return 0
    try:
        return int(json.loads(value))
    except (TypeError, ValueError):
        return 0


async def _write_cursor(uow, value: int, key: str = _CURSOR_KEY) -> None:
    """Persist the resume point (its own short transaction).

    Called *after* the corresponding rows are committed, never before: a
    crash must only re-run a batch, never skip one.
    """
    import json

    from sqlalchemy import delete as _delete
    from copixiv.db import models

    payload = json.dumps(value)

    async def _store(uw) -> None:
        session = uw.session
        session.execute(
            _delete(models.Setting).where(models.Setting.key == key)
        )
        session.add(models.Setting(key=key, value=payload))

    await run_write_transaction(uow, _store)


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
        epub_path = formats.sibling_path(txt_path, formats.EPUB)
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


# ---------------------------------------------------------------------------
# fix_series_index —— 章节号 / 系列 id 修复（见任务 docstring）
# ---------------------------------------------------------------------------

# 三个分支各推各的游标。check_epub 的 _CURSOR_KEY 是模块级共享常量，这里绝不复用：
# 一个任务的断点被另一个任务覆盖，等于两边都在瞎跑。
_SERIES_CURSOR_KEY = "fix_series_index.cursor"          # 缺号系列
_ORPHAN_CURSOR_KEY = "fix_series_index.orphan_cursor"   # 孤儿号行（按 novel.id）
_SWEEP_CURSOR_KEY = "fix_series_index.sweep_cursor"     # 全量复查（sweep_all）


class FixSeriesIndexArgs(BaseModel):
    """``fix_series_index`` 的 JSON 参数。

    三个"每轮上限"都可缺省（``None`` = 不限），缺省时行为与旧版一致——一轮把
    候选集走完。任务每轮都要打 pixiv（每个待修 / 待查系列一次名册、每本孤儿一次
    webview），限量只是把一轮切成几轮跑：游标保证不漏行、下轮从断点续。

    **生产必须给上限**（2026-10-07 实测）：不限量 = 一轮 1,243 个缺号系列 + 1,580
    条孤儿，30 分钟的超时跑不完；而超时被杀时整页从未扫完，游标也就一直停在 0，
    下一轮又从同一个系列从头重扫，永远推不完一轮。给了上限，游标每查完一个系列就
    落一次，超时 / 中断都能从断点续。

    ``sweep_all`` 打开"全量复查"：把库里号齐全的系列也拉名册核对一遍
    （生产口径 25,069 个系列 ≈ 2.5 万次请求，一次跑不完就靠它自己的游标续）。
    默认关闭——那时只扫"有缺号"的系列（1,179 个）与孤儿行（1,594 行）。
    """

    max_series: int | None = None     # 本轮最多处理的"缺号系列"数
    max_orphans: int | None = None    # 本轮最多处理的"孤儿号"行数
    max_sweep: int | None = None      # 本轮最多复查的"号齐全系列"数
    sweep_all: bool = False           # 是否开启"号齐全系列"的全量复查

    @field_validator("max_series", "max_orphans", "max_sweep", mode="before")
    @classmethod
    def _blank_means_unlimited(cls, value):
        """空串按"不限"处理。

        任务编辑器对"默认 null"的参数会填空串
        （``TaskEditModal.vue``：``arg.default !== null ? arg.default : ''``），
        不转换的话，用户没碰过的输入框会让整个任务参数校验失败。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sweep_all", mode="before")
    @classmethod
    def _blank_means_off(cls, value):
        """同理：空串按"关闭"处理（手改 params JSON 时会遇到）。"""
        if isinstance(value, str) and not value.strip():
            return False
        return value


def _page(ids: list[int], cap: int | None) -> tuple[list[int], bool]:
    """游标之后的候选 *ids* 与本轮分页：返回 ``(本轮要跑的页, 是否被上限截断)``。"""
    if cap is None:
        return ids, False
    return ids[:cap], len(ids) > cap


async def _stored_columns(uow, novel_id: int) -> dict | None:
    """一行的库内现值（只读），没有这一行则 None。

    只改一两列的 draft 必须带上这些列的现值：``repo._upsert_rows`` 的覆盖白名单
    （like / view / title / text / caption / series_name）是**无条件写入**的，
    draft 里少了哪一列就等于把它清成 NULL / 0。
    """
    async with uow.begin():
        return await SQLAlchemySeriesRepository(uow.session).novel_columns(novel_id)


def _draft_from_row(columns: dict, **overrides):
    """以库内现值为底造 draft（``has_epub=None`` = 不碰 EPUB 状态列）。

    这是本任务**唯一**的 draft 来源。刻意不从 ``novel_series`` 的名册条目造：
    那个载荷顶层是 ``pixivpy3.utils.ParsedJson``、条目是 ``JsonDict``（dict 子类，
    ``__getattr__`` 回落到键访问），``build_from_novel_info`` 的属性访问在生产能
    跑通纯属上游实现细节——换成普通 dict 就 ``AttributeError``；而且以条目为底
    造 draft 时，条目缺哪一列就会把库里那一列洗掉。名册条目只用来取号。
    """
    from copixiv.core.draft import NovelDraft

    values = {**columns, **overrides}
    values.pop("has_epub", None)
    return NovelDraft(has_epub=None, **values)


@register(
    "fix_series_index",
    description="修复小说章节号与系列归属的数据问题",
    args=FixSeriesIndexArgs,
)
async def fix_series_index(args: FixSeriesIndexArgs, ctx: TaskContext) -> TaskResult:
    """修复「小说缺章节号」与「丢了系列 id」两类数据问题。

    ``novel.series_index`` 只在**下载那一刻**从 webview 的
    ``seriesNavigation.prevNovel/nextNovel.contentOrder`` 推算
    （``core/services.py::guess_series_order``）；元数据刷新路径永远拿不到号——
    pixiv 的 ``novelInfo`` / ``novel_series`` / novel detail 三个端点都不返回
    index 字段，``core/draft.py::build_from_novel_info`` 读的 ``series.index``
    因此恒为空。漏掉的号只能靠后补，本任务就是那个"后补"。

    ① **编号公式（2026-09-24 用系列 16242552 全 9 本实测钉死）**：``novel_series``
       的返回顺序就是作者的章节顺序（**从旧到新**），**章节号 = 名册 1 基位置**
       （第 1 本 → 1，第 N 本 → N）。实测对照：名册
       ``[28670265, 28701995, 28943643, 28761898, 28820329, 28886104, 29049733,
       29092404, 29173929]`` ↔ 导航权威号 1..9 ↔ 标题 第一幕 / 第二幕 / 第2.5幕 /
       第三幕 / 第四幕 / 第五幕 / 第5.5幕 / 第六幕 / 第七幕。注意这条顺序**既不是**
       id 序**也不是** create_time 序（第 3 位"第2.5幕"插在第二幕之后，而它的 id
       和发布时间都比第 4 位晚）——旧版 ``novels.sort(key=id)`` 正是把这条作者顺序
       覆盖掉，才写出 6.8% 的错值。

    ② **缺号**（有系列、号为空）：拉名册，按上面的公式补号；同一批次里号已经是
       错值的行也一并纠正——纠正必须 ``force_update=["series_index"]``
       （``repo._upsert_rows`` 只在"列是 NULL"或"列进了 ``update_fields_set``"时
       才写，``series_index`` 不在后者的默认集合里）。draft **以库内现值为底**
       （``_stored_columns`` + ``_draft_from_row``），名册条目**只用来取号**：
       条目在生产是 ``JsonDict``、测试里可能是普通 dict 或 pydantic 对象，
       ``safe_get`` 是三种形状都成立的唯一取法；从条目造 draft 则条目缺哪列就洗掉
       库里哪列。只有"号为空或与权威值不一致"的行才下写，不整系列无脑 UPDATE。

    ③ **孤儿号**（``series_id`` 为空、``series_index`` 还留着）：2026-07-13 之前
       那版元数据刷新把 ``series_id`` 抹成 NULL 的化石行（生产实测 1,594 行，全部
       ``create_time`` 更早），``series_with_empty_index()`` 永远扫不到它们。逐本
       ``webview_novel`` 直查，按 id 走独立游标：

       * ``series_id`` 有值 → 并进名册批次挂回：身份列（``series_id`` /
         ``series_name`` / 作者）取自 **webview 响应**（``safe_get``，允许覆盖
         NULL），号由名册算出的权威值决定；库里没有别的行挂在这个系列下、且导航
         能算出号时退化为单本推算，省一次名册请求。
       * ``series_id`` 为 NULL → 作品确实已不在任何系列：清号（写 None，同样要
         ``force_update``，否则"非空者胜"的填充规则写不动它），``series_name``
         **保留不动**——编辑器那条"解除关联"是用户主动清（连名字一起清，因为清的是
         本地书），这里清的是"来源已经没有系列"的化石行，名字留着仍是这本书的信息
         （``docs/NOVEL_EDIT_DESIGN.md`` §5.6-3：「有名无 id 的行不归编辑器管」）。
       * 请求失败 / 404 / 响应解析不出来 → **什么都不改**：行留在候选集里、游标
         不越过它，下一轮先重试（不吞异常，也不假装已处理）。

    ④ **错值全量复查**（``sweep_all=true``，默认关闭）：旧版只扫"有缺号"的系列，
       "号齐全但值是错的"系列（生产实测 1,538 个系列、约 9,780 行）永远没人管。
       开启后按 ``repo.series_with_stored_index``（至少一本带号）逐系列拉名册核对，
       复用同一条纠正路径与 ``force_update=["series_index"]``。它与缺号系列候选
       **互补**（并集 = 库里全部 26,248 个系列、互不重叠），所以一轮全量复查约
       2.5 万次名册请求——30 分钟的超时跑不完，靠 ``_SWEEP_CURSOR_KEY`` 续跑。

    三个分支各有独立游标（``setting`` 表，:data:`_SERIES_CURSOR_KEY` /
    :data:`_ORPHAN_CURSOR_KEY` / :data:`_SWEEP_CURSOR_KEY`）：**先落库、再推进
    游标**，而且**每查完一个系列（成功或失败）都推**——失败不是"没查"，是"查过了、
    这轮拿不到"；不推的话，配上"每轮上限"，一页开头一个永久失败行（已删作品 /
    受限系列）就会把游标钉住，后面的行永远轮不到。一轮走完候选集就归零，下一轮从
    头复查，失败的行仍在候选集里，所以会被再查一遍——"跳过"只持续到下一次整轮
    复查。每个系列一次提交，超时 / 中断不丢进度。默认禁用
    （``scheduled_task.is_enabled`` 列默认 false，显示名「修复章节」）。

    开销账（2026-09 生产口径）：默认一轮 = 1,179 个缺号系列 + 1,594 条孤儿 ≈
    2.8k 次请求；``sweep_all`` 再叠加 25,069 次名册请求。名册一轮内按系列缓存
    （三个分支的候选常常是同一个系列），空跑一轮（候选集为空）一次请求都不发。
    """
    from copixiv.core.services import guess_series_order, safe_get
    from copixiv.features.novels.ingest import batch_upsert

    uow = ctx.uow

    series_cursor = await _read_cursor(uow, key=_SERIES_CURSOR_KEY)
    orphan_cursor = await _read_cursor(uow, key=_ORPHAN_CURSOR_KEY)
    sweep_cursor = await _read_cursor(uow, key=_SWEEP_CURSOR_KEY)

    # ---- 候选集：缺号系列（②）+ 孤儿号行（③）+ 号齐全系列（④，sweep_all）----
    async with uow.begin():
        repo = SQLAlchemySeriesRepository(uow.session)
        candidates = sorted(await repo.series_with_empty_index())
        orphan_candidates = await repo.novels_with_orphan_index(
            limit=(
                args.max_orphans + 1 if args.max_orphans is not None else None
            ),
            after_id=orphan_cursor,
        )
        sweep_candidates = (
            await repo.series_with_stored_index(
                after_id=sweep_cursor,
                limit=(
                    args.max_sweep + 1 if args.max_sweep is not None else None
                ),
            )
            if args.sweep_all
            else []
        )

    series_page, series_truncated = _page(
        [sid for sid in candidates if sid > series_cursor], args.max_series,
    )
    orphan_truncated = (
        args.max_orphans is not None and len(orphan_candidates) > args.max_orphans
    )
    orphan_page = (
        orphan_candidates[:args.max_orphans]
        if args.max_orphans is not None
        else orphan_candidates
    )
    sweep_truncated = (
        args.max_sweep is not None and len(sweep_candidates) > args.max_sweep
    )
    sweep_page = (
        sweep_candidates[:args.max_sweep]
        if args.max_sweep is not None
        else sweep_candidates
    )

    if not series_page and not orphan_page and not sweep_page:
        # 候选集为空：一次网络请求都不发。游标停在半路（上轮已走完 / 行已被修好）
        # 就归零，让下一轮从头复查。
        if series_cursor:
            await _write_cursor(uow, 0, key=_SERIES_CURSOR_KEY)
        if orphan_cursor:
            await _write_cursor(uow, 0, key=_ORPHAN_CURSOR_KEY)
        if sweep_cursor:
            await _write_cursor(uow, 0, key=_SWEEP_CURSOR_KEY)
        return TaskResult(summary="系列章节号检查: 无需修复")

    stats = {
        "filled": 0, "fixed": 0, "glued": 0, "cleared": 0,
        "series_done": 0, "orphan_done": 0, "sweep_done": 0, "failed": 0,
    }

    # 一轮内的名册缓存：同一个系列一轮只拉一次名册（② / ③ / ④ 的候选常常是同一个
    # 系列，去重后一次补一批）。
    rosters: dict[int, list | None] = {}

    async def _roster(series_id: int) -> list | None:
        """该系列的名册（作者章节顺序：从旧到新）；None = 本轮拿不到。"""
        if series_id not in rosters:
            try:
                resp = await ctx.client.novel_series(series_id, fetch_all=True)
            except Exception as exc:                     # 网络 / 限流 / 404
                logger.warning(
                    f"fix_series_index: 系列 {series_id} 名册请求失败: {exc}"
                )
                rosters[series_id] = None
            else:
                novels = safe_get(resp, "novels", None)
                rosters[series_id] = list(novels) if novels else None
        return rosters[series_id]

    async def _apply_roster(
        series_id: int,
        roster: list,
        forced: dict[int, tuple[dict, str | None, int | None]],
    ) -> tuple[int, int, set[int]]:
        """把名册的权威号落库（一个系列一次提交）。

        权威号 = **名册位置（1 基）**：``novel_series`` 的返回顺序就是作者的章节
        顺序（从旧到新，2026-09-24 用系列 16242552 全 9 本实测钉死）。

        返回 ``(补空数, 纠正数, 实际挂回的 id 集合)``。*forced* 是"孤儿挂回"的
        ``{id: (库内现值列, webview 给的系列名, webview 给的作者 id)}``：这些行的
        ``series_id`` 还是 NULL，不在"库内挂在该系列下"的名单里，要单独走一批带
        身份列的 ``force_update``；身份来自 webview 响应，不从名册条目取。

        draft 一律以**库内现值**为底（``_draft_from_row``），名册条目只用来取号
        ——条目缺列不会洗掉 title / text / tags，也不依赖上游对象类型。
        """
        authority: dict[int, int] = {}
        for pos0, item in enumerate(roster):
            item_id = safe_get(item, "id")
            if item_id is None:
                continue
            authority[int(item_id)] = pos0 + 1          # 名册位置（1 基）= 章节号

        async with uow.begin():
            stored = await SQLAlchemySeriesRepository(uow.session).novels_of_series(
                series_id
            )

        fills: list = []      # 号为空 → 直接补（不需要 force_update）
        fixes: list = []      # 号是错值 → 必须 force_update 才写得进去
        for columns in stored:
            novel_id = int(columns["id"])
            want = authority.get(novel_id)
            if want is None:
                continue            # 名册里没有这一本（受限 / 未收录）→ 不动它
            draft = _draft_from_row(columns, series_index=want)
            if not draft.author_id:
                continue            # 连作者都没有：写不进去（见 _write_glue_single）
            if columns["series_index"] is None:
                fills.append(draft)
            elif int(columns["series_index"]) != want:
                fixes.append(draft)

        glue: list = []
        glued: set[int] = set()
        for novel_id, (columns, series_name, author_id) in forced.items():
            want = authority.get(novel_id)
            if want is None:
                continue            # 名册里没有它 → 交给调用方退化为单本推算
            draft = _draft_from_row(
                columns,
                author_id=columns.get("author_id") or author_id,
                series_id=series_id,
                series_name=series_name or columns.get("series_name"),
                series_index=want,
            )
            if not draft.author_id:
                continue
            glue.append(draft)
            glued.add(novel_id)

        if fills or fixes or glue:
            async def _write(uw) -> None:
                if fills:
                    await batch_upsert(fills, uw)
                if fixes:
                    await batch_upsert(fixes, uw, force_update=["series_index"])
                if glue:
                    await batch_upsert(
                        glue, uw,
                        force_update=["series_id", "series_name", "series_index"],
                    )

            await run_write_transaction(uow, _write)

        return len(fills), len(fixes), glued

    async def _write_glue_single(
        columns: dict, series_id: int, series_name: str | None,
        index: int | None, author_id: int | None,
    ) -> bool:
        """单本挂回（系列里只此一本 / 名册里没有它），不拉名册。

        号用 webview 导航推算（``guess_series_order``，与下载那一刻同一个算法）；
        导航给不出号时**不动**这一列。draft 以库内现值为底，只改系列三列。
        *author_id* 是 webview 给的作者兜底：库里这一列是 NULL 时按"只补空"规则
        填上（生产实测 1,594 本孤儿里有 13 本没有 author_id，否则根本写不进去——
        ``persist_novels`` 会把 None 拿去建作者占位行，撞 NOT NULL）。
        """
        author_id = columns.get("author_id") or author_id
        if not author_id:
            return False                  # 连作者都缺 → 什么都不改（下轮重试）
        force = ["series_id", "series_name"]
        if index:
            force.append("series_index")
        draft = _draft_from_row(
            columns,
            author_id=author_id,
            series_id=int(series_id),
            series_name=series_name or columns.get("series_name"),
            series_index=index if index else columns.get("series_index"),
        )
        await run_write_transaction(
            uow, lambda uw: batch_upsert([draft], uw, force_update=force),
        )
        return True

    async def _glue_orphan(
        novel_id: int, series_id: int, columns: dict, data,
        series_name: str | None, author_id: int | None,
    ) -> tuple[bool, int, int]:
        """把一本孤儿挂回系列：优先并进名册批次，必要时退化为单本推算。

        身份三列（series_id / series_name / 作者）一律取自 **webview 响应**
        （``safe_get``：dict / JsonDict / pydantic 三种形状都成立），不从名册条目取。
        """
        async with uow.begin():
            siblings = await SQLAlchemySeriesRepository(
                uow.session
            ).count_novels_by_series(series_id)
        nav_index = guess_series_order(safe_get(data, "series_navigation"))
        title = safe_get(data, "series_title")
        if not siblings and nav_index:
            # 库里这个系列只有这一本、导航又能算出号 → 省一次名册请求
            ok = await _write_glue_single(
                columns, series_id, title, nav_index, author_id,
            )
            return ok, 0, 0

        roster = await _roster(series_id)
        if roster is None:
            return False, 0, 0
        if not any(int(safe_get(item, "id", 0) or 0) == novel_id for item in roster):
            # 名册里没有它（受限 / 未收录）→ 别猜位置，退化为单本推算
            ok = await _write_glue_single(
                columns, series_id, title, nav_index, author_id,
            )
            return ok, 0, 0

        filled, fixed, glued = await _apply_roster(
            series_id, roster, {novel_id: (columns, title, author_id)},
        )
        return novel_id in glued, filled, fixed

    async def _clear_orphan_index(
        columns: dict, author_id: int | None = None,
    ) -> bool:
        """作品已不在任何系列 → 清章节号（``series_name`` 保留不动）。"""
        author_id = columns.get("author_id") or author_id
        if not author_id:
            return False                  # 连作者都缺 → 什么都不改（下轮重试）
        await run_write_transaction(
            uow,
            lambda uw: batch_upsert(
                [_draft_from_row(columns, author_id=author_id, series_index=None)],
                uw,
                force_update=["series_index"],
            ),
        )
        return True

    async def _run_roster_branch(
        page: list[int], cursor: int, key: str, counter: str, truncated: bool,
    ) -> None:
        """逐系列拉名册 → 落库 → 推游标（缺号系列与全量复查共用的骨架）。

        游标语义：**先落库、再推进**，而且**每查完一个系列（成功或失败）都推**。

        为什么失败也推：失败不是"没查"，是"查过了、这轮拿不到"。失败不推时，配上
        "每轮上限"，一页开头一个永久失败行（已删作品 / 受限系列）就会把游标钉在
        原地，后面的行永远轮不到（2026-10-07 生产实测，task_history id=1489：任务
        从系列 1244949 扫到 15764828 后 30 分钟超时被杀，游标因整页从未扫完而一直
        是 0，每天从头重来）。失败的行仍在候选集里，跑完一轮归零后会再复查，所以
        "跳过"只持续到下一次整轮复查。
        """
        next_cursor = cursor
        for series_id in page:
            next_cursor = series_id
            roster = await _roster(series_id)
            if roster is None:
                stats["failed"] += 1
            else:
                filled, fixed, _ = await _apply_roster(series_id, roster, {})
                stats["filled"] += filled
                stats["fixed"] += fixed
                stats[counter] += 1
            # 每查完一个系列就落一次游标：超时 / 中断也保住这一页的进度，下一轮从
            # 断点续，而不是重扫整页。
            await _write_cursor(uow, next_cursor, key=key)

        if not truncated and (cursor or page):
            # 候选集走完了 → 归零，下一轮从头复查（失败的行还在候选集里）。
            await _write_cursor(uow, 0, key=key)

    # ---- ② 缺号系列：名册权威序补号 + 顺带纠正错值 ----
    await _run_roster_branch(
        series_page, series_cursor, _SERIES_CURSOR_KEY,
        "series_done", series_truncated,
    )

    # ---- ③ 孤儿号：series_id 被抹成 NULL，号还留着 ----
    orphan_next = orphan_cursor
    for novel_id, series_name, _index in orphan_page:
        orphan_next = novel_id
        columns = await _stored_columns(uow, novel_id)
        if columns is None:
            # 行没了 = 没处理（也不值得为它打一次 pixiv）。照推游标：它是永久失败，
            # 留着只会饿死后面的孤儿行。
            stats["failed"] += 1
            await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)
            continue
        try:
            data = await ctx.client.webview_novel(novel_id)
        except Exception as exc:                         # 网络 / 限流 / 404
            logger.warning(
                f"fix_series_index: #{novel_id} webview 请求失败: {exc}"
            )
            data = None
        if not data:
            # 空响应 / 解析不出来：什么都不改，但**游标照样推**——否则配上"每轮
            # 上限"，开头一条永久失败行会把后面的孤儿永远饿死。失败行仍在候选集
            # 里，跑完一轮归零后再复查。
            stats["failed"] += 1
            await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)
            continue

        series_id = safe_get(data, "series_id")
        author_id = safe_get(data, "user_id")     # 库里 author_id 为 NULL 时的兜底
        if series_id:
            ok, filled, fixed = await _glue_orphan(
                novel_id, int(series_id), columns, data, series_name, author_id,
            )
            stats["filled"] += filled
            stats["fixed"] += fixed
            stats["glued"] += 1 if ok else 0
        else:
            ok = await _clear_orphan_index(columns, author_id)
            stats["cleared"] += 1 if ok else 0
        if not ok:
            stats["failed"] += 1
        else:
            stats["orphan_done"] += 1
        await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)

    if not orphan_truncated and (orphan_cursor or orphan_page):
        await _write_cursor(uow, 0, key=_ORPHAN_CURSOR_KEY)

    # ---- ④ 错值全量复查（sweep_all）：号齐全的系列也拉名册核对一遍 ----
    if args.sweep_all:
        await _run_roster_branch(
            sweep_page, sweep_cursor, _SWEEP_CURSOR_KEY,
            "sweep_done", sweep_truncated,
        )

    if (
        stats["series_done"] == 0
        and stats["orphan_done"] == 0
        and stats["sweep_done"] == 0
    ):
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    summary = (
        f"系列章节号修复: 补号 {stats['filled']} 本, 纠正 {stats['fixed']} 本, "
        f"孤儿挂回 {stats['glued']} 本, 孤儿清号 {stats['cleared']} 本 "
        f"(系列 {stats['series_done']}, 孤儿 {stats['orphan_done']}, "
        f"复查 {stats['sweep_done']}, 失败 {stats['failed']})"
    )
    if series_truncated or orphan_truncated or sweep_truncated:
        summary += "；本轮达上限，下轮续跑"
    return TaskResult(summary=summary)


@register("rebuild_tag_counts", description="重算每个标签的引用计数")
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
