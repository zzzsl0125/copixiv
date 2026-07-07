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
) -> list[str]:
    """Process a batch of novels: metadata upsert for existing, download for new.

    When *client* / *file_storage* / *image_downloader* are provided,
    new novels are downloaded concurrently via :func:`_download_novels`
    and upserted to the DB.  Without them (legacy callers), only metadata
    upsert is performed.

    Returns the titles of newly downloaded novels.
    """
    if not novels:
        return []

    ids = {n.id for n in novels}
    existing = await uow.novels.get_existing_ids(ids)
    need_download = ids if redownload else ids - existing

    logger.info(
        f"_batch_handle: {len(novels)} novels — "
        f"{len(existing)} already in DB, {len(need_download)} need download",
    )

    titles: list[str] = []

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
            )
            if downloaded:
                await _batch_upsert(downloaded, uow)
                titles = [d["title"] for d in downloaded if "title" in d]
                logger.info(
                    f"_batch_handle: {len(downloaded)} novels downloaded and upserted",
                )
        else:
            logger.warning(
                f"_batch_handle: {len(download_ids)} novels need download "
                f"but no client/storage provided — skipping download",
            )

    return titles


# ---------------------------------------------------------------------------
# Per-page handler factory
# ---------------------------------------------------------------------------


def _make_page_handler(
    session_factory,
    client,
    file_storage,
    image_downloader,
    redownload: bool = False,
):
    """Build a per-page handler with its own UoW session.

    Each page from a paginated API call runs concurrently — sharing a single
    session across concurrent handlers causes SQLite write contention.  This
    factory creates handlers that each get their own session.
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
                return await _batch_handle(
                    cn, page_uow,
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                    redownload=redownload,
                )

    return handler
