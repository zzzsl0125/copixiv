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

    **Full table every run, chunked for the event loop.**  The first full pass
    over production measured ~10.5 ms/row — 42 minutes for 238 k rows — because
    every row still had to be *classified*; that is what the old bounded-slice
    + cursor design was built for.  After the one-off classification the
    per-row cost collapsed (terminal rows cost a stat, not a read): a full pass
    now measures 5.0 s on the production table, 1.1 s for a complete
    ``check_epub`` run on 200 k rows, and 1.2 min even in the worst case where
    every row needs its text read.

    So the cursor is gone.  It advanced after every chunk and reset only when
    ``id > cursor`` returned nothing — which, with new novels arriving daily,
    almost never happened.  Everything below the cursor was never examined
    again while the task reported 「全表完成」: 1 594 production rows sat
    stranded that way (684 with a vanished source file, 910 pending).  The scan
    is still chunked and still applies every chunk as it goes (a killed run
    repeats work, never stranding rows — the sweep is idempotent).
    """
    from sqlalchemy import select as _select
    from copixiv.db import models

    uow = ctx.uow
    started = time.monotonic()

    # **Full-table scan every run.**  This used to read ``id > cursor`` with a
    # 60 000-row cap, advancing the cursor after each chunk and resetting it
    # only when the query returned *nothing*.  Because new novels arrive every
    # day, the "nothing" branch almost never fires, so the cursor only ever
    # moved forward: everything below it was never examined again, while the
    # task happily reported 「全表完成」.
    #
    # Why the cap is now pointless: the steady-state sweep is cheap by
    # construction.  Terminal ``NO_IMAGES`` rows cost one ``stat`` (they skip
    # the text read unless a readable EPUB appeared), ``DONE`` rows cost one
    # zip check, and only rows that can still change state read the body.
    # Measured on the production 246 035-row table: 5.0 s for a full pass
    # (0.02 ms/row), 1.1 s for a complete ``check_epub`` run on a 200 k-row
    # test table, and 1.2 min even in the worst case where every row needs its
    # text read.  All of that is far below the 30-minute task timeout.
    #
    # Dropping the cursor removes the entire starvation class instead of
    # patching the wrap-around: there is no "below the cursor" any more, so a
    # bounded slice can never strand rows.  An interrupted run simply repeats
    # work next time — the sweep is idempotent.
    async with uow.begin():
        rows = uow.session.execute(
            _select(models.Novel.id, models.Novel.path, models.Novel.has_epub)
            .order_by(models.Novel.id)
        ).fetchall()

    # A leftover cursor from the old design would make the next reader think
    # part of the table was already done; clear it once, then forget it.
    if await _read_cursor(uow):
        await _write_cursor(uow, 0)

    if not rows:
        return TaskResult(summary="EPUB 状态检查: 无需修复（空库）")


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
    missing_ids: list[int] = []
    totals = {
        "completed": 0, "reverted": 0, "enabled": 0,
        "no_images": 0, "missing": 0,
    }

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
        if missing_ids:
            await _apply(missing_ids, EpubStatus.MISSING_SOURCE)
            totals["missing"] += len(missing_ids)
            missing_ids.clear()

    for start in range(0, len(rows), _SWEEP_CHUNK):
        chunk = rows[start:start + _SWEEP_CHUNK]
        (
            c_done, c_revert, c_noimg, c_enable, c_pending, c_missing,
        ) = await asyncio.to_thread(_sweep, chunk, start)
        completed_ids += c_done
        revert_ids += c_revert
        no_image_ids += c_noimg
        enable_ids += c_enable
        pending_ids += c_pending
        missing_ids += c_missing
        await _flush()
        # Explicit yield: give the HTTP handlers a turn between chunks.
        await asyncio.sleep(0)

    logger.info(
        f"check_epub: 本轮 {len(rows)} 行（{time.monotonic() - started:.0f}s），"
        f"completed={totals['completed']}, reverted={totals['reverted']}, "
        f"enabled={totals['enabled']}, no_images={totals['no_images']}, "
        f"missing={totals['missing']}, pending={len(pending_ids)}（全表）"
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
    if totals["missing"]:
        parts.append(f"{totals['missing']} 本源文件丢失（需重新下载）")
    if pending_ids:
        parts.append(f"{len(pending_ids)} 本仍待处理")
    # "scanned but nothing changed" and "nothing to scan" are different facts;
    # the old wording collapsed both into 「无变化」, which read as "all good"
    # even on a run that examined 3 843 rows and touched none of them.
    return TaskResult(
        summary=(
            "EPUB 状态检查: "
            + (" ".join(parts) if parts else "本轮无变化")
            + f"（全表 {len(rows)} 行）"
        )
    )



_CURSOR_KEY = "check_epub.cursor"

# Retained only so the one-time cursor cleanup in ``check_epub`` can clear a
# ``setting`` row left behind by the old bounded-slice design.  Nothing reads
# the cursor to decide what to scan any more — see the task docstring.
_LEGACY_CURSOR_KEY = _CURSOR_KEY


def _sweep(
    rows: list, offset: int = 0,
) -> tuple[list[int], list[int], list[int], list[int], list[int], list[int]]:
    """Classify *rows* against the files on disk (runs in a worker thread).

    Pure file I/O + decisions — no database access, so it can be offloaded
    with :func:`asyncio.to_thread` without touching the session.  *offset*
    is the index of the first row within the whole table, used only so the
    progress log reports absolute positions.  Returns
    ``(completed, reverted, no_images, enabled, pending, missing)`` id lists.
    """
    completed_ids: list[int] = []
    revert_ids: list[int] = []
    no_image_ids: list[int] = []
    enable_ids: list[int] = []
    pending_ids: list[int] = []
    missing_ids: list[int] = []

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

        # No local text at all: nothing can ever be *built* from this row, so
        # it must not sit in ``0``/``1`` claiming work is pending.  This is the
        # whole 684-row production backlog: metadata written, body never
        # downloaded, no EPUB, no image, no failure record.  Classifying it is
        # this task's job; re-downloading is the UI's (``MISSING_SOURCE`` is
        # terminal for the builder, not for the fetch path).
        if not txt_path.is_file():
            if status != EpubStatus.MISSING_SOURCE:
                missing_ids.append(novel_id)
            continue

        if status == EpubStatus.DONE:
            # A file that is not a valid zip is as good as missing — two
            # such novels were served to readers for months.
            if not epub_ok:
                revert_ids.append(novel_id)
                continue
            # "Readable" is not "finished": an EPUB whose XHTML still carries
            # raw ``[pixivimage:…]`` markers is served to the reader as literal
            # text.  DONE must mean ``state.is_complete`` (markers resolved AND
            # an illustration embedded) — the same predicate every other
            # promotion uses, so the reader and the reconciler cannot disagree.
            if state.is_complete(epub_path):
                continue
            # Readable but unfinished: decide where it belongs instead of
            # leaving it advertised as done.
            canonical = _txt_has_images(txt_path)
            if canonical is False:
                # Nothing to embed — the (superfluous) EPUB is not a reason to
                # keep a book in the build queue.
                no_image_ids.append(novel_id)
            elif canonical is None:
                revert_ids.append(novel_id)   # unreadable text → rebuildable
            elif _no_images_ever_and_stale(txt_path, novel_id):
                no_image_ids.append(novel_id)  # images gone for good
            else:
                revert_ids.append(novel_id)   # rebuild it properly
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
            elif _no_images_ever_and_stale(txt_path, novel_id):
                # A readable shell whose illustrations never landed, with the
                # text untouched for _STALE_DAYS: the images are gone for good
                # (author deleted them / the URLs are dead).  The "give up"
                # rule used to be reachable only from the ``not epub_ok``
                # branch, so these rows stayed PENDING forever — 905 of them
                # in production, re-judged every night and never resolved.
                #
                # ORDER IS LOAD-BEARING: this must stay *after*
                # ``state.is_complete``.  A finished, image-less EPUB whose
                # text file happens to be stale also satisfies the stale
                # predicate (one production row does exactly that), and
                # demoting it to NO_IMAGES would destroy a perfectly good book.
                no_image_ids.append(novel_id)
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

    return (
        completed_ids, revert_ids, no_image_ids, enable_ids, pending_ids,
        missing_ids,
    )


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

    **A built EPUB that still carries raw markers vetoes giving up.**  The
    verdict this rule feeds is "nothing will ever be built for this row", so
    it must not fire while the file on disk is *known to be broken* — those
    rows need a rebuild, not a terminal state.  Without the veto, the
    producer/consumers mismatch (builder returning True for an image-less
    shell) upgraded 5 production books from "wrongly DONE" straight to
    "never rebuilt", leaving literal ``[uploadedimage:…]`` visible to
    readers.  The check is a zip walk, but this branch is only reached for
    rows whose body has placeholders and which have no image file — a few
    hundred rows at most.
    """
    parent = txt_path.parent
    if any(parent.glob(f"{novel_id}_u_*")) or any(parent.glob(f"{novel_id}_p_*")):
        return False
    epub_path = formats.sibling_path(txt_path, formats.EPUB)
    if epub_path.is_file() and state.has_raw_markers(epub_path):
        return False
    try:
        age_days = (time.time() - txt_path.stat().st_mtime) / 86400
    except OSError:
        return False
    return age_days > _STALE_DAYS
