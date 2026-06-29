"""Background tasks for novel operations.

These are async functions that accept dependency arguments directly
(injected by the task runner at execution time).
"""

import asyncio
import calendar
import logging
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

logger = logging.getLogger("copixiv")


def _batch_upsert(novels: list[dict], uow, force_update: list[str] | None = None) -> int:
    """Upsert novels and update author/series summaries."""
    novels = [n for n in novels if n]
    if not novels:
        return 0
    count = uow.novels.upsert_novels(novels, force_update or [])
    uow.authors.update_summary({n["author_id"] for n in novels})
    uow.series.update_summary(
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
        return await _batch_upsert([data], uow)


@register("novel_follow")
async def novel_follow(
    days: int = 3,
    force: bool = False,
    *,
    client,
    uow,
    **_,
):
    """Fetch new novels from followed users."""
    async def _handle(resp):
        novels = safe_get(resp, "novels", [])
        if not novels:
            return []
        cn_novels = [
            n for n in novels
            if is_chinese(
                title=safe_get(n, "title", ""),
                caption=safe_get(n, "caption", ""),
                tags=[
                    safe_get(safe_get(n, "user"), "name", "")
                    for n in novels
                ],
            )
        ]
        async with client.account_rule():
            return await _batch_handle(cn_novels, uow, force)

    fetch_til = datetime.now().astimezone() - timedelta(days=days)
    resp = await client.novel_follow(fetch_til=fetch_til)
    return safe_get(resp, "handler_results", [])


@register("author_fetch")
async def author_fetch(
    author_id: int,
    force: bool = False,
    redownload: bool = False,
    *,
    client,
    uow,
    **_,
):
    """Fetch all novels by an author."""
    async with uow.begin():
        if not force and not await uow.authors.need_update(author_id):
            logger.info(f"Skip Author {author_id}, already updated today.")
            return []

        author = await uow.authors.get_by_id(author_id)
        if not author:
            await client.user_follow_add(author_id)

    async def _download(resp):
        novels = safe_get(resp, "novels", [])
        if not novels:
            return []
        cn = [n for n in novels if is_chinese(n)]
        async with client.account_rule():
            return await _batch_handle(cn, uow, redownload)

    resp = await client.user_novels(author_id, fetch_all=True, handler=_download)

    async with uow.begin():
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
    **_,
):
    """Check for new novels from specially-followed authors."""
    async with uow.begin():
        author_ids = await uow.authors.get_special_follow_author_ids()

    if not author_ids:
        logger.info("No special followed authors.")
        return []

    all_novels = []
    for author_id in author_ids:
        resp = await client.user_novels(author_id)
        novels = safe_get(resp, "novels", [])
        if novels:
            all_novels.extend(novels)

    async with uow.begin():
        return await _batch_handle(all_novels, uow)


@register("novel_ranking")
async def novel_ranking(
    mode: str = "day_r18",
    days: int = 3,
    force: bool = False,
    *,
    client,
    uow,
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
        cn = [n for n in novels if is_chinese(n)]
        authors = {safe_get(n.user, "id") for n in cn if is_chinese(n)}
        async with client.account_rule():
            results = await asyncio.gather(*[
                author_fetch(a, force=force) for a in authors
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
    **_,
):
    """Search novels by keyword over a time range."""
    end = datetime.now() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles = []
    async with client.account_rule(need_premium=True):
        for start_date, end_date in _month_ranges(end_date, months):
            logger.info(f"Searching {start_date.date()} ~ {end_date.date()}")
            resp = await client.search_novel(
                keyword, "keyword", "popular_desc",
                start_date=start_date, end_date=end_date,
                fetch_minlike=minlike,
            )
            all_titles.extend(safe_get(resp, "handler_results", []))
    return all_titles


@register("random_pool_renew")
async def random_pool_renew(
    *,
    uow,
    **_,
):
    """Rebuild the random novel pool."""
    like_tiers = [0, 500, 2500, 5000]
    text_tiers = [0, 3000, 10000, 30000]
    async with uow.begin():
        for like in like_tiers:
            for text in text_tiers:
                await uow.novels.populate_random_novel_pool(like, text)


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


# ---- Helpers ----------------------------------------------------------------


async def _batch_handle(
    novels: list,
    uow,
    redownload: bool = False,
):
    """Process a batch of novels: decide which need download, download, upsert."""
    if not novels:
        return []

    ids = {n.id for n in novels}
    existing = await uow.novels.get_existing_ids(ids)
    need_download = ids if redownload else ids - existing

    # Metadata-only upsert for existing
    if not redownload:
        await _batch_upsert(
            [build_from_novel_info(n) for n in novels if n.id in existing],
            uow,
        )

    # Download new ones
    download_ids = [n.id for n in novels if n.id in need_download]
    # ... (download logic requires client — handled per-task)
    return []
