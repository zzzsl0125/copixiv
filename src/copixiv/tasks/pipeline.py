"""Download + persistence pipeline shared by task functions.

These are the building blocks that registered tasks compose together:
batch upserter, concurrent downloader, plan/persist phases, and
small pure helpers for filtering / date-range generation.
"""

import asyncio
import calendar
from datetime import datetime, timedelta
from pathlib import Path

from copixiv.infrastructure.database.write_lock import db_write

from copixiv.domain.models.novel import Novel
from copixiv.domain.services.language import is_chinese
from copixiv.application.novel.download_novel import fetch_novel_and_assets
from copixiv.application.novel.persist import persist_novels

from copixiv.domain.services.novel_factory import (
    NovelInfoLike, build_from_novel_info,
)
from copixiv.domain.services.parsing import safe_get

from copixiv.app.logger import logger


# ---------------------------------------------------------------------------
# Pure helpers (no side effects)
# ---------------------------------------------------------------------------


def _account(key: str, config=None) -> str:
    if config is None:
        return ""
    return getattr(config.pixiv_accounts, key, "")


def _month_ranges(end_date: datetime, months: int):
    for offset in range(months):
        if offset == 0:
            yield end_date.replace(day=1), end_date
        else:
            y, m = end_date.year, end_date.month
            for _ in range(offset):
                m -= 1
                if m == 0:
                    m = 12
                    y -= 1
            last_day = calendar.monthrange(y, m)[1]
            yield datetime(y, m, 1), datetime(y, m, last_day)


def _filter_chinese_novels(novels: list) -> list:
    """Return only Chinese-language novels from a Pixiv API response list."""
    return [
        n for n in novels
        if is_chinese(
            title=safe_get(n, "title", ""),
            caption=safe_get(n, "caption", ""),
            tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
        )
    ]


# ---------------------------------------------------------------------------
# Batch upsert
# ---------------------------------------------------------------------------


async def _batch_upsert(
    novels: list[Novel], uow, force_update: list[str] | None = None,
) -> int:
    """Upsert novels and update author/series summaries.

    Thin wrapper over :func:`copixiv.application.novel.persist.persist_novels`
    that adds batch-level logging.

    Pure write helper: the caller is responsible for wrapping it in
    ``db_write()`` + ``uow.begin()`` so the whole batch (including the
    commit) happens while holding the global write lock.
    """
    count = await persist_novels(uow, novels, force_update)
    novels = [n for n in novels if n]
    if novels:
        logger.info(
            f"_batch_upsert: {count} newly inserted out of "
            f"{len(novels)} input novels (sample id: {novels[0].id!r})"
        )
    return count


# ---------------------------------------------------------------------------
# Concurrent download
# ---------------------------------------------------------------------------


async def _download_novels(
    novel_ids: list[int],
    client,
    file_storage,
    image_downloader,
    redownload: bool = False,
) -> tuple[list[Novel], list[tuple[int, str]]]:
    """Download novels via webview concurrently.

    Fires all webview_novel calls in parallel — the client semaphore + LRU
    account selection distribute the load across accounts automatically.
    Each novel's text, images, and EPUB are processed as it completes.

    Pure download phase: no database access at all (the write lock is
    never held across network I/O).  Failed downloads are returned as
    ``(novel_id, reason)`` records for the caller to persist inside the
    write transaction.
    """
    if not novel_ids:
        return [], []

    async def _fetch_one(nid: int) -> Novel | None:
        data = await fetch_novel_and_assets(
            nid, client, file_storage, image_downloader, redownload,
        )
        if data is None:
            logger.warning(f"下载: #{nid} webview 返回空")
        return data

    results = await asyncio.gather(
        *[_fetch_one(nid) for nid in novel_ids],
        return_exceptions=True,
    )

    valid: list[Novel] = []
    failed_records: list[tuple[int, str]] = []
    for nid, result in zip(novel_ids, results):
        if isinstance(result, Exception):
            logger.error(
                f"下载: #{nid} 失败: {type(result).__name__}: {result}"
            )
            failed_records.append((nid, str(result)))
        elif result is not None:
            valid.append(result)

    logger.info(
        f"_download_novels: {len(valid)}/{len(novel_ids)} downloaded successfully",
    )
    return valid, failed_records


# ---------------------------------------------------------------------------
# Batch handle (metadata + download)
# ---------------------------------------------------------------------------


async def _plan_batch(
    novels: list[NovelInfoLike],
    uow,
    redownload: bool = False,
    failed_repo=None,
) -> tuple[list[Novel], list[int]]:
    """Plan phase (read-only): decide what to download and what to upsert.

    Must run outside the write lock — it only reads.  Returns
    ``(existing_meta, download_ids)`` where ``existing_meta`` holds the
    metadata dicts of already-known novels (empty when ``redownload``),
    and ``download_ids`` are the novel IDs that still need downloading.
    """
    if not novels:
        return [], []

    ids = {n.id for n in novels}
    existing = await uow.novels.get_existing_ids(ids)

    # Exclude novels that have already failed too many times
    skip_ids: set[int] = set()
    if failed_repo is not None:
        need_check = ids if redownload else ids - existing
        skip_ids = failed_repo.get_skip_ids(need_check)

    need_download = ids if redownload else ids - existing - skip_ids

    logger.info(
        f"_plan_batch: {len(novels)} novels — "
        f"{len(existing)} in DB, {len(skip_ids)} failed-too-many-times, "
        f"{len(need_download)} need download",
    )

    existing_meta: list[Novel] = []
    if not redownload:
        existing_meta = [
            build_from_novel_info(n) for n in novels if n.id in existing
        ]

    download_ids = [n.id for n in novels if n.id in need_download]
    return existing_meta, download_ids


async def _persist_batch(
    existing_meta: list[Novel],
    downloaded: list[Novel],
    uow,
    failed_records: list[tuple[int, str]] | None = None,
    failed_repo=None,
) -> tuple[list[str], set[int]]:
    """Persist phase (write-only): run inside ``db_write()`` + ``uow.begin()``.

    Records download failures, upserts existing metadata + downloaded
    novels, and forgets success markers — all in the caller's write
    transaction.  Returns ``(titles, new_author_ids)``.
    """
    titles: list[str] = []
    new_author_ids: set[int] = set()

    # Failed downloads — recorded in the same write transaction.
    if failed_records and failed_repo is not None:
        for nid, reason in failed_records:
            failed_repo.record(nid, "download", reason)

    # Metadata-only upsert for existing
    if existing_meta:
        upserted = await _batch_upsert(existing_meta, uow)
        logger.info(
            f"_persist_batch: metadata upsert done for {upserted} existing novels",
        )

    if downloaded:
        success_ids = {d.id for d in downloaded}
        await _batch_upsert(downloaded, uow)
        if failed_repo is not None:
            failed_repo.forget_many(success_ids)
        titles = [d.title for d in downloaded if d.title]
        new_author_ids = {d.author_id for d in downloaded if d.author_id}
        logger.info(
            f"_persist_batch: {len(downloaded)} novels downloaded and upserted",
        )

    return titles, new_author_ids


async def _batch_handle(
    novels: list[NovelInfoLike],
    session_factory,
    client=None,
    file_storage=None,
    image_downloader=None,
    redownload: bool = False,
) -> tuple[list[str], set[int]]:
    """Process a batch of novels end-to-end: plan → download → persist.

    The pipeline keeps every database write inside ``db_write()`` and
    never holds a transaction (or the write lock) across network
    downloads.  Concurrent calls are safe: the plan phase reads without
    the lock, the download phase touches no database, and the persist
    phase serializes through ``db_write()``.

    Returns ``(titles, new_author_ids)`` — the titles and author IDs of
    newly downloaded novels (which may have missing author names because
    the webview API doesn't return them).  Callers should pass
    ``new_author_ids`` to :func:`resolve_author_names` to fill in the gaps.
    """
    if not novels:
        return [], set()

    from copixiv.infrastructure.database.uow import SqlUnitOfWork

    # 1. Plan — read-only, short transaction, no lock.
    uow = SqlUnitOfWork(session_factory)
    async with uow.begin():
        existing_meta, download_ids = await _plan_batch(
            novels, uow, redownload=redownload, failed_repo=uow.failed_novels,
        )

    # 2. Download — concurrent network/file I/O, no database.
    downloaded: list[Novel] = []
    failed_records: list[tuple[int, str]] = []
    if download_ids:
        if client and file_storage and image_downloader:
            downloaded, failed_records = await _download_novels(
                download_ids, client, file_storage, image_downloader,
                redownload=redownload,
            )
        else:
            logger.warning(
                f"_batch_handle: {len(download_ids)} novels need download "
                f"but no client/storage provided — skipping download",
            )

    # 2.5 Gate — wait for in-flight image/EPUB tasks before persisting, so
    # the write transaction only sees novels whose files are actually on
    # disk (no "downloaded but EPUB not ready yet" race).  Failures are
    # collected and persisted in the same transaction as the downloads.
    asset_failures: list[tuple[int, str]] = []
    if image_downloader is not None:
        asset_failures = await image_downloader.await_all()
        if asset_failures:
            logger.warning(
                f"_batch_handle: {len(asset_failures)} novels failed "
                f"image/EPUB processing",
            )

    # 3. Persist — one write transaction inside the global write lock.
    async with db_write():
        async with uow.begin():
            titles, new_author_ids = await _persist_batch(
                existing_meta, downloaded, uow,
                failed_records=failed_records + asset_failures,
                failed_repo=uow.failed_novels,
            )

    return titles, new_author_ids

