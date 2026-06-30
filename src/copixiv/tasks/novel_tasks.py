"""Background tasks for novel operations.

These are async functions that accept dependency arguments directly
(injected by the task runner at execution time).
"""

import asyncio
import calendar
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from copixiv.domain.services.language import is_chinese
from copixiv.domain.services.novel_factory import (
    build_from_webview,
    build_from_novel_info,
)
from copixiv.domain.services.parsing import safe_get, safe_set
from copixiv.domain.services.filename import build_path as _build_path
from copixiv.domain.services.archive import build_batch_zip
from copixiv.domain.services.parsing import parse_search_keyword

from .registry import register

from copixiv.app.logger import logger


async def _batch_upsert(novels: list[dict], uow, force_update: list[str] | None = None) -> int:
    """Upsert novels and update author/series summaries."""
    novels = [n for n in novels if n]
    if not novels:
        return 0

    # Ensure author + series placeholder rows exist before novel insert.
    # Without these, the FK constraints on novel.author_id / novel.series_id
    # will fail when a novel is the first one seen for that author/series.
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from copixiv.infrastructure.database import models as _m

    author_ids = {n["author_id"] for n in novels}
    for aid in author_ids:
        uow.session.execute(
            sqlite_insert(_m.Author)
            .values(author_id=aid)
            .on_conflict_do_nothing()
        )
    series_ids = {sid for n in novels if (sid := n.get("series_id"))}
    for sid in series_ids:
        uow.session.execute(
            sqlite_insert(_m.Series)
            .values(series_id=sid)
            .on_conflict_do_nothing()
        )
    uow.session.flush()

    count = await uow.novels.upsert_novels(novels, force_update or [])
    logger.info(
        f"_batch_upsert: upsert_novels returned {count} for "
        f"{len(novels)} input novels (sample id: {novels[0].get('id')!r})"
    )
    await uow.authors.update_summary({n["author_id"] for n in novels})
    await uow.series.update_summary(
        {sid for n in novels if (sid := n.get("series_id"))}
    )
    return count


def _account(key: str, config) -> str:
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


# ---- Individual tasks -------------------------------------------------------


@register("novel_fetch")
async def novel_fetch(
    id: int,
    redownload: bool = True,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    **_,
):
    """Download and persist a single novel by ID."""
    resp = await client.webview_novel(id)
    if resp is None:
        return 0
    data = build_from_webview(resp, file_storage.download_dir)

    if content := data.pop("content", None):
        file_storage.save_novel_text(data["id"], data["title"], content, force=redownload)

    await image_downloader.process_novel_assets(data, force=redownload)

    async with uow.begin():
        count = await _batch_upsert([data], uow)
        if count:
            # Webview API doesn't include author name — fetch it separately.
            try:
                detail = await client.user_detail(data["author_id"])
                name = safe_get(safe_get(detail, "user", {}), "name", "")
                if name:
                    await uow.authors.update_author_name(data["author_id"], name)
            except Exception:
                logger.warning(
                    f"Failed to fetch name for author #{data['author_id']}"
                )
        return count


@register("novel_follow")
async def novel_follow(
    days: int = 3,
    force: bool = False,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    **_,
):
    """Fetch new novels from followed users."""
    # Each page handler runs concurrently — must use per-page sessions.
    from copixiv.infrastructure.database.uow import SqlUnitOfWork as _UoW
    _sf = uow._session_factory

    async def _handle(resp):
        novels = safe_get(resp, "novels", [])
        if not novels:
            return []
        cn_novels = [
            n for n in novels
            if is_chinese(
                title=safe_get(n, "title", ""),
                caption=safe_get(n, "caption", ""),
                tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
            )
        ]
        async with client.account_rule():
            page_uow = _UoW(_sf)
            async with page_uow.begin():
                return await _batch_handle(
                    cn_novels, page_uow,
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                    redownload=force,
                )

    fetch_til = datetime.now().astimezone() - timedelta(days=days)
    resp = await client.novel_follow(fetch_til=fetch_til, handler=_handle)
    return safe_get(resp, "handler_results", [])


@register("author_fetch")
async def author_fetch(
    author_id: int,
    force: bool = False,
    redownload: bool = False,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    config,
    **_,
):
    """Fetch all novels by an author."""
    async with uow.begin():
        if not force and not await uow.authors.need_update(author_id):
            logger.info(f"Skip Author {author_id}, already updated today.")
            return []

        author = await uow.authors.get_by_id(author_id)
        if not author:
            async with client.account_rule(
                force_account=_account("follow", config),
            ):
                await client.user_follow_add(author_id)

    # Each page handler runs concurrently and MUST have its own session.
    # Sharing a single session across concurrent handlers causes SQLite
    # write contention → "database is locked".
    from copixiv.infrastructure.database.uow import SqlUnitOfWork as _UoW
    _sf = uow._session_factory

    async def _download(resp):
        novels = safe_get(resp, "novels", [])
        if not novels:
            return []
        cn = [n for n in novels if is_chinese(
            title=safe_get(n, "title", ""),
            caption=safe_get(n, "caption", ""),
            tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
        )]
        async with client.account_rule():
            page_uow = _UoW(_sf)
            async with page_uow.begin():
                return await _batch_handle(
                    cn, page_uow,
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                    redownload=redownload,
                )

    resp = await client.user_novels(author_id, fetch_all=True, handler=_download)

    # Fetch author name (webview API doesn't return it) and persist
    # to both the author row and all novels by this author.
    async with uow.begin():
        try:
            detail = await client.user_detail(author_id)
            name = safe_get(safe_get(detail, "user", {}), "name", "")
            if name:
                await uow.authors.update_author_name(author_id, name)
                logger.info(f"Author #{author_id} name set to {name!r}")
        except Exception:
            logger.warning(f"Failed to fetch name for author #{author_id}")
        await uow.authors.update_last_update(author_id)

    return safe_get(resp, "handler_results", [])


@register("author_delete")
async def author_delete(
    author_id: int,
    *,
    client,
    uow,
    **_,
):
    """Delete an author and all their novels."""
    async with uow.begin():
        await uow.authors.delete_author_and_data(author_id)

    async with client.account_rule(
        force_account=_account("follow")
    ):
        return await client.user_follow_delete(author_id)


@register("author_special_follow")
async def author_special_follow(
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    **_,
):
    """Check for new novels from specially-followed authors."""
    async with uow.begin():
        author_ids = await uow.authors.get_special_follow_author_ids()

    if not author_ids:
        logger.info("No special followed authors.")
        return []

    # Fan out: fetch novels from all authors concurrently.
    # Each call goes through the client semaphore (max 5) and picks
    # the LRU account, so up to 5 accounts call the API in parallel.
    responses = await asyncio.gather(
        *[client.user_novels(aid) for aid in author_ids],
        return_exceptions=True,
    )

    all_novels = []
    for author_id, resp in zip(author_ids, responses):
        if isinstance(resp, Exception):
            logger.error(f"Failed to fetch novels for author {author_id}: {resp}")
            continue
        novels = safe_get(resp, "novels", [])
        if novels:
            all_novels.extend(novels)

    async with uow.begin():
        return await _batch_handle(
            all_novels, uow,
            client=client,
            file_storage=file_storage,
            image_downloader=image_downloader,
        )


@register("novel_ranking")
async def novel_ranking(
    mode: str = "day_r18",
    days: int = 3,
    force: bool = False,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    **_,
):
    """Fetch novel rankings."""
    titles = []
    for delta in range(1, max(2, days)):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(mode=mode, date=target, fetch_all=True)
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        cn = [n for n in novels if is_chinese(
            title=safe_get(n, "title", ""),
            caption=safe_get(n, "caption", ""),
            tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
        )]
        authors = {safe_get(n.user, "id") for n in cn}
        async with client.account_rule():
            results = await asyncio.gather(*[
                author_fetch(
                    a, force=force,
                    client=client, uow=uow,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                )
                for a in authors
            ])
        titles.extend(t for r in results for t in r)
    return titles


@register("novel_search")
async def novel_search(
    keyword: str = "R-18",
    months: int = 1,
    minlike: int = 500,
    force: bool = False,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    **_,
):
    """Search novels by keyword over a time range.

    Pagination accumulates results in ``resp.novels`` (no handler needed).
    For each Chinese novel found, the author's full catalogue is fetched
    via :func:`author_fetch` — same pattern as :func:`novel_ranking`.
    """
    end = datetime.now() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles: list[str] = []
    async with client.account_rule(need_premium=True):
        for start_date, end_date in _month_ranges(end_date, months):
            logger.info(f"Searching {start_date.date()} ~ {end_date.date()}")
            resp = await client.search_novel(
                keyword, "keyword", "popular_desc",
                start_date=start_date, end_date=end_date,
                fetch_minlike=minlike,
            )
            novels = safe_get(resp, "novels", [])
            if not novels:
                continue
            cn = [n for n in novels if is_chinese(
                title=safe_get(n, "title", ""),
                caption=safe_get(n, "caption", ""),
                tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
            )]
            authors = {
                safe_get(safe_get(n, "user"), "id")
                for n in cn if safe_get(n, "user")
            }
            if authors:
                async with client.account_rule():
                    results = await asyncio.gather(*[
                        author_fetch(
                            a, force=force,
                            client=client, uow=uow,
                            file_storage=file_storage,
                            image_downloader=image_downloader,
                        )
                        for a in authors
                    ])
                all_titles.extend(t for r in results for t in r)
    return all_titles


@register("rebuild_fts")
async def rebuild_fts(
    *,
    uow,
    **_,
):
    """Rebuild the FTS5 index."""
    async with uow.begin():
        await uow.novels.rebuild_fts()


@register("batch_download")
async def batch_download_task(
    keyword: str = "",
    min_like: int = 0,
    min_text: int = 0,
    order_by: str = "id",
    order_direction: str = "DESC",
    limit: int = 50,
    fmt: str = "txt",
    *,
    uow,
    config,
    **_,
):
    """Batch download: zip matching novels and save to disk."""
    queries = parse_search_keyword(keyword)

    async with uow.begin():
        results = await uow.novels.get_novels(
            queries=queries or None,
            order_by=order_by,
            order_direction=order_direction,
            per_page=limit,
            min_like=min_like if min_like > 0 else None,
            min_text=min_text if min_text > 0 else None,
        )

    novels = results.get("novels", [])
    if not novels:
        logger.info("batch_download: 未找到匹配条件的小说")
        return []

    zip_buffer, titles, _ = build_batch_zip(novels, fmt)
    if not titles:
        return []

    download_dir = Path(config.path.download or "downloads")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"batch_download_{len(titles)}篇_{timestamp}.zip"
    zip_path = download_dir / zip_filename
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(zip_buffer.getvalue())

    logger.info(f"batch_download: 已保存 {zip_path} ({len(titles)} 篇)")
    return titles


# ---- Maintenance tasks ------------------------------------------------------


@register("sync_empty_name")
async def sync_empty_name(
    *,
    client,
    uow,
    **_,
):
    """Fix novels whose ``author_name`` is NULL.

    First tries to resolve names from the local ``author`` table, then
    falls back to the Pixiv API for any remaining authors.
    """
    from sqlalchemy import select as _select, update as _update
    from copixiv.infrastructure.database import models

    async with uow.begin():
        stmt = _select(models.Novel).where(models.Novel.author_name.is_(None))
        novels = uow.session.execute(stmt).scalars().all()

    if not novels:
        return []

    author_novels: dict[int, list[int]] = {}
    for n in novels:
        author_novels.setdefault(n.author_id, []).append(n.id)

    author_names: dict[int, str] = {}
    async with uow.begin():
        for author_id in author_novels:
            author = await uow.authors.get_by_id(author_id)
            if author and author.get("author_name"):
                author_names[author_id] = author["author_name"]

    missing = [a for a in author_novels if a not in author_names]
    if missing:
        results = await asyncio.gather(
            *[client.user_detail(a) for a in missing],
            return_exceptions=True,
        )
        for author_id, result in zip(missing, results):
            if not isinstance(result, Exception):
                user = safe_get(result, "user", {})
                name = safe_get(user, "name", "")
                if name:
                    author_names[author_id] = name

    async with uow.begin():
        for author_id, name in author_names.items():
            if not name:
                continue
            nids = author_novels.get(author_id, [])
            if nids:
                uow.session.execute(
                    _update(models.Novel)
                    .where(models.Novel.id.in_(nids))
                    .values(author_name=name)
                )
        await uow.authors.update_summary(set(author_novels.keys()))
        await uow.series.update_summary(
            await uow.series.get_empty_series_ids()
        )

    return list(author_names.values())


@register("check_epub")
async def check_epub(
    *,
    uow,
    **_,
):
    """Synchronise ``has_epub`` status with actual files on disk.

    * 1 (pending) + file exists  → 2 (completed)
    * 2 (completed) + file gone  → 1 (pending)
    * 1 (pending) + try download if file missing
    """
    from sqlalchemy import select as _select
    from copixiv.infrastructure.database import models

    async with uow.begin():
        stmt = _select(
            models.Novel.id, models.Novel.path, models.Novel.has_epub
        ).where(models.Novel.has_epub > 0)
        rows = uow.session.execute(stmt).fetchall()

    if not rows:
        return []

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

    return completed_ids + revert_ids + pending_ids


# ---- Helpers ----------------------------------------------------------------


async def _download_novels(
    novel_ids: list[int],
    client,
    file_storage,
    image_downloader,
    redownload: bool = False,
) -> list[dict]:
    """Download novels via webview concurrently (like V1's _download_and_handle).

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


async def _batch_handle(
    novels: list,
    uow,
    client=None,
    file_storage=None,
    image_downloader=None,
    redownload: bool = False,
):
    """Process a batch of novels: metadata upsert for existing, download for new.

    When *client* / *file_storage* / *image_downloader* are provided,
    new novels are downloaded concurrently via :func:`_download_novels`
    and upserted to the DB.  Without them (legacy callers), only metadata
    upsert is performed.
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
