"""EPUB status reconciliation and disk-scan helpers.

Everything here except the async DB round-trips runs in a worker thread
(:func:`asyncio.to_thread`) — pure file I/O and state-machine decisions, no DB
access inside :func:`_sweep`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.models import EpubStatus, TaskResult
from copixiv.core.text import MARKER_OVERLAP, has_image_placeholders_bytes
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.storage import assets
from copixiv.storage.epub import state

_STALE_DAYS = 7

# Rows classified per worker-thread hop.  Small enough that the event loop
# gets a turn (and HTTP keeps being served) several times a second, large
# enough that the `to_thread` overhead stays negligible.
_SWEEP_CHUNK = 4000

# How many rows one keyset page reads (~20 k tuples ≈ 6 MB peak).
_SWEEP_READ_PAGE = 20_000


async def reconcile_epub_status(uow: SqlUnitOfWork) -> TaskResult:
    """Synchronise ``has_epub`` status with actual files on disk."""
    started = time.monotonic()

    async def _page(after_id: int | None) -> list:
        return await read_transaction(
            uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).iter_epub_sweep_page(after_id, _SWEEP_READ_PAGE),
        )

    pending_count = 0
    totals = {
        "completed": 0,
        "reverted": 0,
        "enabled": 0,
        "no_images": 0,
        "missing": 0,
    }

    async def _apply(ids: list[int], status: EpubStatus, *, clear_failures: bool = False) -> None:
        def _write(uw: SqlUnitOfWork) -> None:
            SQLAlchemyNovelRepository(uw.session).update_has_epub_status(ids, status)
            if clear_failures:
                FailedNovelRepository(uw.session).forget_many(set(ids))

        await run_write_transaction(uow, _write)

    examined = 0
    seen = 0
    after_id: int | None = None
    while True:
        rows = await _page(after_id)
        if not rows:
            break
        after_id = rows[-1][0]
        seen += len(rows)

        for start in range(0, len(rows), _SWEEP_CHUNK):
            chunk = rows[start : start + _SWEEP_CHUNK]
            (
                c_done,
                c_revert,
                c_noimg,
                c_enable,
                c_pending,
                c_missing,
            ) = await asyncio.to_thread(_sweep, chunk, examined)
            examined += len(chunk)
            pending_count += len(c_pending)
            for ids, status, key, clear_failures in (
                (c_done, EpubStatus.DONE, "completed", True),
                (c_revert, EpubStatus.PENDING, "reverted", False),
                (c_enable, EpubStatus.PENDING, "enabled", False),
                (c_noimg, EpubStatus.NO_IMAGES, "no_images", True),
                (c_missing, EpubStatus.MISSING_SOURCE, "missing", False),
            ):
                if ids:
                    await _apply(ids, status, clear_failures=clear_failures)
                    totals[key] += len(ids)
            # Explicit yield: give HTTP handlers a turn between chunks.
            await asyncio.sleep(0)

    if not seen:
        return TaskResult(summary="EPUB 状态检查: 无需修复（空库）")

    logger.info(
        f"check_epub: 本轮 {seen} 行（{time.monotonic() - started:.0f}s），"
        f"completed={totals['completed']}, reverted={totals['reverted']}, "
        f"enabled={totals['enabled']}, no_images={totals['no_images']}, "
        f"missing={totals['missing']}, pending={pending_count}（全表）"
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
    if pending_count:
        parts.append(f"{pending_count} 本仍待处理")
    return TaskResult(
        summary=("EPUB 状态检查: " + (" ".join(parts) if parts else "本轮无变化") + f"（全表 {seen} 行）")
    )


def _sweep(
    rows: list,
    offset: int = 0,
) -> tuple[list[int], list[int], list[int], list[int], list[int], list[int]]:
    """Classify *rows* against the files on disk (runs in a worker thread)."""
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
        if not txt_path.is_file():
            if status != EpubStatus.MISSING_SOURCE:
                missing_ids.append(novel_id)
            continue

        epub_path = formats.sibling_path(txt_path, formats.EPUB)
        epub_ok, epub_complete = state.inspect_epub(epub_path)

        # 本地上传书（负 ID）不由 Pixiv 图片目录规则判定完整性，仅校验可读容器即可
        if novel_id < 0:
            if status == EpubStatus.DONE and not epub_ok:
                no_image_ids.append(novel_id)
            elif status == EpubStatus.NO_IMAGES and epub_ok:
                completed_ids.append(novel_id)
            continue

        if status == EpubStatus.DONE:
            if not epub_ok:
                revert_ids.append(novel_id)
                continue
            if epub_complete:
                continue
            canonical = _scan_placeholder(txt_path)
            if canonical is False:
                no_image_ids.append(novel_id)
            elif canonical is None:
                revert_ids.append(novel_id)
            elif _no_images_ever_and_stale(txt_path, novel_id):
                no_image_ids.append(novel_id)
            else:
                revert_ids.append(novel_id)
            continue

        if status == EpubStatus.NO_IMAGES and not epub_ok:
            continue

        canonical = _scan_placeholder(txt_path)

        if status == EpubStatus.NO_IMAGES:
            if epub_complete:
                completed_ids.append(novel_id)
            continue

        if status == EpubStatus.PENDING:
            if not epub_ok:
                if canonical is False or _no_images_ever_and_stale(txt_path, novel_id):
                    no_image_ids.append(novel_id)
                else:
                    pending_ids.append(novel_id)
            elif canonical is False:
                no_image_ids.append(novel_id)
            elif canonical is None:
                pending_ids.append(novel_id)
            elif epub_complete:
                completed_ids.append(novel_id)
            elif _no_images_ever_and_stale(txt_path, novel_id):
                # ORDER IS LOAD-BEARING: must stay *after* epub_complete.
                no_image_ids.append(novel_id)
            else:
                pending_ids.append(novel_id)
            continue

        # status == NO (unclassified, or any unexpected value).
        if canonical is False:
            no_image_ids.append(novel_id)
        elif canonical is None:
            continue
        elif not epub_complete:
            enable_ids.append(novel_id)
        else:
            completed_ids.append(novel_id)

    return (
        completed_ids,
        revert_ids,
        no_image_ids,
        enable_ids,
        pending_ids,
        missing_ids,
    )


def _scan_placeholder(txt_path: Path) -> bool | None:
    """Scan *txt_path* for an image placeholder without decoding it."""
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


def _no_images_ever_and_stale(txt_path: Path, novel_id: int) -> bool:
    """True when no image file was ever downloaded and the last attempt is stale."""
    if assets.has_illustration_assets(txt_path.parent, novel_id):
        return False
    try:
        age_days = (time.time() - txt_path.stat().st_mtime) / 86400
    except OSError:
        return False
    return age_days > _STALE_DAYS


__all__ = [
    "reconcile_epub_status",
]
