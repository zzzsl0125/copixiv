"""Download + persistence pipeline shared by task functions.

These are the building blocks that registered tasks compose together:
batch upserter, concurrent downloader, per-page handler factory, and
small pure helpers for filtering / date-range generation.
"""

import asyncio
import calendar
from datetime import datetime, timedelta
from pathlib import Path

_db_write_lock = asyncio.Lock()

from copixiv.domain.services.language import is_chinese
from copixiv.domain.services.novel_factory import (
    build_from_webview,
    build_from_novel_info,
)
from copixiv.domain.services.parsing import safe_get

from copixiv.infrastructure.repositories.failed_novel import FailedNovelRepository

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
    novels: list[dict], uow, force_update: list[str] | None = None,
) -> int:
    """Upsert novels and update author/series summaries.

    Ensures author + series placeholder rows exist before the novel
    insert so FK constraints are satisfied for first-seen authors/series.
    """
    novels = [n for n in novels if n]
    if not novels:
        return 0

    author_ids = {n["author_id"] for n in novels}
    series_ids = {sid for n in novels if (sid := n.get("series_id"))}

    # Serialize all DB writes across concurrent page handlers.
    # SQLite (even in WAL mode) allows only one writer at a time.
    # The lock must cover both the writes AND the commit so that the
    # next handler doesn't try to write before this one's transaction
    # finishes and releases SQLite's internal write lock.
    async with _db_write_lock:
        uow.authors.ensure_exists(author_ids)
        uow.series.ensure_exists(series_ids)

        count = await uow.novels.upsert_novels(novels, force_update or [])
        logger.info(
            f"_batch_upsert: upsert_novels returned {count} for "
            f"{len(novels)} input novels (sample id: {novels[0].get('id')!r})"
        )
        await uow.authors.update_summary({n["author_id"] for n in novels})
        await uow.series.update_summary(
            {sid for n in novels if (sid := n.get("series_id"))}
        )

        # Commit inside the lock so SQLite's write lock is released
        # before the next concurrent handler tries to write.
        await uow.commit()

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
    failed_repo: FailedNovelRepository | None = None,
) -> list[dict]:
    """Download novels via webview concurrently.

    Fires all webview_novel calls in parallel — the client semaphore + LRU
    account selection distribute the load across accounts automatically.
    Each novel's text, images, and EPUB are processed as it completes.
    Returns a list of processed novel dicts ready for DB upsert.
    """
    if not novel_ids:
        return []

    async def _fetch_one(nid: int) -> dict | None:
        resp = await client.webview_novel(nid)
        if resp is None:
            logger.warning(f"下载: #{nid} webview 返回空")
            return None
        data = build_from_webview(resp, file_storage.download_dir)
        if content := data.pop("content", None):
            file_storage.save_novel_text(
                data["id"], data["title"], content, force=redownload,
            )
        await image_downloader.process_novel_assets(data, force=redownload)
        return data

    results = await asyncio.gather(
        *[_fetch_one(nid) for nid in novel_ids],
        return_exceptions=True,
    )

    valid: list[dict] = []
    for nid, result in zip(novel_ids, results):
        if isinstance(result, Exception):
            logger.error(
                f"下载: #{nid} 失败: {type(result).__name__}: {result}"
            )
            if failed_repo is not None:
                failed_repo.record(nid, "download", str(result))
        elif result is not None:
            valid.append(result)

    logger.info(
        f"_download_novels: {len(valid)}/{len(novel_ids)} downloaded successfully",
    )
    return valid


# ---------------------------------------------------------------------------
# Batch handle (metadata + download)
# ---------------------------------------------------------------------------


async def _batch_handle(
    novels: list,
    uow,
    client=None,
    file_storage=None,
    image_downloader=None,
    redownload: bool = False,
    failed_repo: FailedNovelRepository | None = None,
) -> tuple[list[str], set[int]]:
    """Process a batch of novels: metadata upsert for existing, download for new.

    When *client* / *file_storage* / *image_downloader* are provided,
    new novels are downloaded concurrently via :func:`_download_novels`
    and upserted to the DB.  Without them (legacy callers), only metadata
    upsert is performed.

    Returns ``(titles, new_author_ids)`` — the titles and author IDs of
    newly downloaded novels (which may have missing author names because
    the webview API doesn't return them).  Callers should pass
    ``new_author_ids`` to :func:`resolve_author_names` to fill in the gaps.
    """
    if not novels:
        return [], set()

    ids = {n.id for n in novels}
    existing = await uow.novels.get_existing_ids(ids)

    # Exclude novels that have already failed too many times
    skip_ids: set[int] = set()
    if failed_repo is not None:
        need_check = ids if redownload else ids - existing
        skip_ids = failed_repo.get_skip_ids(need_check)

    need_download = ids if redownload else ids - existing - skip_ids

    logger.info(
        f"_batch_handle: {len(novels)} novels — "
        f"{len(existing)} in DB, {len(skip_ids)} failed-too-many-times, "
        f"{len(need_download)} need download",
    )

    titles: list[str] = []
    new_author_ids: set[int] = set()

    # Metadata-only upsert for existing
    if not redownload:
        n_existing = [build_from_novel_info(n) for n in novels if n.id in existing]
        if n_existing:
            upserted = await _batch_upsert(n_existing, uow)
            logger.info(
                f"_batch_handle: metadata upsert done for {upserted} existing novels",
            )

    # Download new ones concurrently
    download_ids = [n.id for n in novels if n.id in need_download]
    if download_ids:
        if client and file_storage and image_downloader:
            downloaded = await _download_novels(
                download_ids, client, file_storage, image_downloader,
                redownload=redownload,
                failed_repo=failed_repo,
            )
            if downloaded:
                success_ids = {d["id"] for d in downloaded}
                await _batch_upsert(downloaded, uow)
                if failed_repo is not None:
                    failed_repo.forget_many(success_ids)
                titles = [d["title"] for d in downloaded if "title" in d]
                new_author_ids = {d["author_id"] for d in downloaded if d.get("author_id")}
                logger.info(
                    f"_batch_handle: {len(downloaded)} novels downloaded and upserted",
                )
        else:
            logger.warning(
                f"_batch_handle: {len(download_ids)} novels need download "
                f"but no client/storage provided — skipping download",
            )

    return titles, new_author_ids


# ---------------------------------------------------------------------------
# Per-page handler factory
# ---------------------------------------------------------------------------


def _make_page_handler(
    session_factory,
    client,
    file_storage,
    image_downloader,
    redownload: bool = False,
    *,
    author_ids_out: set[int] | None = None,
):
    """Build a per-page handler with its own UoW session.

    Each page from a paginated API call runs concurrently — sharing a single
    session across concurrent handlers causes SQLite write contention.  This
    factory creates handlers that each get their own session.

    If *author_ids_out* is provided, the handler will collect the author IDs
    of newly-downloaded novels into it (for later name resolution).
    """
    from copixiv.infrastructure.database.uow import SqlUnitOfWork as _UoW

    async def handler(resp):
        novels = safe_get(resp, "novels", [])
        if not novels:
            return []
        cn = _filter_chinese_novels(novels)
        async with client.account_rule():
            page_uow = _UoW(session_factory)
            async with page_uow.begin():
                failed_repo = FailedNovelRepository(page_uow.session)
                titles, new_author_ids = await _batch_handle(
                    cn, page_uow,
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                    redownload=redownload,
                    failed_repo=failed_repo,
                )
                if author_ids_out is not None:
                    author_ids_out.update(new_author_ids)
                return titles

    return handler
