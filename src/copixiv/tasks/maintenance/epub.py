"""EPUB status reconciliation (``check_epub``) and its disk-scan helpers.

Split verbatim out of ``copixiv.tasks.maintenance`` (see that package's
docstring).  The cursor helpers live in :mod:`._cursor` because the other
maintenance tasks share them.

Everything here except the task itself runs in a worker thread
(:func:`asyncio.to_thread`) — pure file I/O and decisions, no DB access.
"""

from pathlib import Path
import asyncio
import time

from copixiv.core import formats
from copixiv.core.models import EpubStatus
from copixiv.core.models import TaskResult
from copixiv.core.services import (
    MARKER_OVERLAP,
    has_image_placeholders_bytes,
)
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.storage.epub import state
from copixiv.log import logger

from ._cursor import _CURSOR_KEY, _read_cursor, _write_cursor
from ..kernel import TaskContext
from ..kernel import register


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

    The EPUB-content scans (:func:`copixiv.storage.epub.state.is_complete` —
    ``has_embedded_images`` + ``not has_raw_markers``) only run for rows that
    need them.

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
        epub_ok = state.is_readable_container(epub_path)

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
            if epub_ok and state.is_complete(epub_path):
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
            elif state.is_complete(epub_path):
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
        elif not epub_ok or not state.is_complete(epub_path):
            # Missing file, or a file written before its images landed
            # (nothing embedded) → queue it for proper (re)generation.
            enable_ids.append(novel_id)
        else:
            completed_ids.append(novel_id)

    return completed_ids, revert_ids, no_image_ids, enable_ids, pending_ids


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
    into Python strings just to look for a marker.  The marker *syntax* comes
    from the single owner (:func:`core.services.has_image_placeholders_bytes`);
    this loop only supplies the overlap so a marker straddling a chunk
    boundary is still detectable.

    ``MARKER_OVERLAP`` bytes are carried between chunks: the longest possible
    prefix of a marker minus one, so any marker starting near the end of one
    chunk is completed by the head of the next.
    """
    overlap = MARKER_OVERLAP
    try:
        with open(txt_path, "rb") as fh:
            tail = b""
            while chunk := fh.read(1 << 20):
                buffer = tail + chunk
                if has_image_placeholders_bytes(buffer):
                    return True
                tail = buffer[-overlap:]
    except OSError:
        return None
    return False


_STALE_DAYS = 7

# Rows classified per worker-thread hop.  Small enough that the event loop
# gets a turn (and HTTP keeps being served) several times a second, large
# enough that the `to_thread` overhead stays negligible.
_SWEEP_CHUNK = 4000


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
