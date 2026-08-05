"""Background tasks for novel operations.

Each function is registered via ``@register`` and receives injected
dependencies (client, uow, file_storage, etc.) from the task runner.

All tasks return a :class:`TaskResult` so the caller (task manager,
notifier) knows **what kind** of work was done — novel discovery vs.
maintenance — and can format notifications accordingly.
"""

import asyncio
from datetime import datetime, timedelta

from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.services.novel_factory import build_from_webview
from copixiv.domain.services.parsing import safe_get
from copixiv.application.author.resolve_names import resolve_author_names

from .registry import register
from .pipeline import (
    _account,
    _batch_upsert,
    _batch_handle,
    _filter_chinese_novels,
    _month_ranges,
)
from . import maintenance  # noqa: F401 — ensure @register decorators fire

from copixiv.infrastructure.database.write_lock import db_write
from copixiv.infrastructure.database.uow import SqlUnitOfWork

from copixiv.app.logger import logger


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


async def _fan_out_author_fetch(
    novels: list,
    *,
    force: bool = False,
    client,
    uow,
    file_storage,
    image_downloader,
    config,
) -> list[str]:
    """Given a list of Pixiv API novel objects, filter to Chinese novels,
    extract unique author IDs, and fan out ``author_fetch`` concurrently.

    Used by :func:`novel_ranking` and :func:`novel_search`.

    Each concurrent ``author_fetch`` gets its own :class:`SqlUnitOfWork`
    — sessions are never shared across coroutines.  All database writes
    are serialized by the global ``db_write()`` lock inside the pipeline,
    so concurrency here only ever covers network I/O.

    Returns the combined list of newly-downloaded novel titles.
    """
    cn = _filter_chinese_novels(novels)
    if not cn:
        return []

    authors = {
        safe_get(n, "user.id")
        for n in cn if safe_get(n, "user")
    }
    if not authors:
        return []

    async with client.account_rule():
        results = await asyncio.gather(*[
            author_fetch(
                a, force=force,
                client=client, uow=SqlUnitOfWork(uow.session_factory),
                file_storage=file_storage,
                image_downloader=image_downloader,
                config=config,
            )
            for a in authors
        ])

    # Each author_fetch returns a TaskResult; flatten their titles.
    titles: list[str] = []
    for r in results:
        if isinstance(r, TaskResult):
            titles.extend(r.new_novel_titles)
    return titles


# ---------------------------------------------------------------------------
# Registered tasks
# ---------------------------------------------------------------------------


@register("novel_fetch")
async def novel_fetch(
    id: int,
    redownload: bool = True,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
):
    """Download and persist a single novel by ID."""
    resp = await client.webview_novel(id)
    if resp is None:
        return TaskResult(summary=f"小说 #{id} 获取失败")

    data = build_from_webview(resp, file_storage.download_dir)

    if content := data.pop("content", None):
        file_storage.save_novel_text(data["id"], data["title"], content, force=redownload)

    await image_downloader.process_novel_assets(data, force=redownload)

    async with db_write():
        async with uow.begin():
            count = await _batch_upsert([data], uow)

    # Resolve author name — webview API doesn't return it.
    if count:
        await resolve_author_names(
            {data["author_id"]}, client=client, uow=uow,
        )

    title = data.get("title", "")
    if count:
        return TaskResult(
            summary=f"下载完成: {title}",
            new_novel_titles=[title],
            new_novel_count=count,
        )
    return TaskResult(summary=f"已存在，跳过: {title}")


@register("novel_follow")
async def novel_follow(
    days: int = 3,
    force: bool = False,
    *,
    client,
    uow,
    file_storage,
    image_downloader,
    config,
):
    """Fetch new novels from followed users.

    Forces the designated "follow" account (config.pixiv_accounts.follow)
    because the Pixiv novel_follow endpoint returns results scoped to the
    authenticated account's own following list.

    Collect-then-persist: the feed is fetched without touching the
    database, then processed by :func:`_batch_handle`.
    """
    fetch_til = datetime.now().astimezone() - timedelta(days=days)
    async with client.account_rule(
        force_account=_account("follow", config),
    ):
        resp = await client.novel_follow(fetch_til=fetch_til)

    novels = _filter_chinese_novels(safe_get(resp, "novels", []))
    titles, new_author_ids = await _batch_handle(
        novels, uow.session_factory,
        client=client, file_storage=file_storage,
        image_downloader=image_downloader, redownload=force,
    )

    if new_author_ids:
        await resolve_author_names(new_author_ids, client=client, uow=uow)

    return TaskResult(
        summary=f"关注更新: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


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
):
    """Fetch all novels by an author.

    Collect-then-persist flow: the whole catalogue is fetched without
    touching the database (client accumulates pages into ``resp.novels``),
    then :func:`_batch_handle` downloads new novels concurrently and
    persists everything in a single write transaction inside
    ``db_write()``.  No transaction is ever held across network I/O, and
    no write happens outside the global write lock.
    """
    session_factory = uow.session_factory

    # Plan — read-only, short transaction, no lock.
    async with uow.begin():
        if not force and not await uow.authors.need_update(author_id):
            logger.info(f"Skip Author {author_id}, already updated today.")
            return TaskResult(summary=f"作者 #{author_id} 今日已更新，跳过")

        author = await uow.authors.get_by_id(author_id)

    if not author:
        async with client.account_rule(
            force_account=_account("follow", config),
        ):
            await client.user_follow_add(author_id)

    # Collect — pure network, pages accumulate into resp.novels.
    resp = await client.user_novels(author_id, fetch_all=True)
    novels = _filter_chinese_novels(safe_get(resp, "novels", []))

    # Persist — plan → download → write, writes serialized by db_write().
    titles, _new_author_ids = await _batch_handle(
        novels, session_factory,
        client=client, file_storage=file_storage,
        image_downloader=image_downloader, redownload=redownload,
    )

    # Mark the author as updated today (same write discipline).
    async with db_write():
        async with uow.begin():
            await uow.authors.update_last_update(author_id)

    # Resolve author name — webview API doesn't return it.
    resolved = await resolve_author_names(
        {author_id}, client=client, uow=uow,
    )
    name = resolved.get(author_id, "")

    label = name or f"#{author_id}"
    return TaskResult(
        summary=f"作者 {label}: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


@register("author_delete")
async def author_delete(
    author_id: int,
    *,
    client,
    uow,
    config,
):
    """Delete an author and all their novels."""
    async with db_write():
        async with uow.begin():
            await uow.authors.delete_author_and_data(author_id)

    async with client.account_rule(
        force_account=_account("follow", config)
    ):
        await client.user_follow_delete(author_id)

    return TaskResult(summary=f"已删除作者 #{author_id} 及其全部小说")


@register("author_special_follow")
async def author_special_follow(
    *,
    client,
    uow,
    file_storage,
    image_downloader,
):
    """Check for new novels from specially-followed authors."""
    async with uow.begin():
        author_ids = await uow.authors.get_special_follow_author_ids()

    if not author_ids:
        logger.info("No special followed authors.")
        return TaskResult(summary="无特别关注的作者")

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

    titles, new_author_ids = await _batch_handle(
        all_novels, uow.session_factory,
        client=client, file_storage=file_storage,
        image_downloader=image_downloader,
    )

    if new_author_ids:
        await resolve_author_names(new_author_ids, client=client, uow=uow)

    return TaskResult(
        summary=f"特别关注: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
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
    config,
):
    """Fetch novel rankings."""
    titles: list[str] = []
    for delta in range(1, max(2, days)):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(mode=mode, date=target, fetch_all=True)
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        t = await _fan_out_author_fetch(
            novels, force=force,
            client=client, uow=uow,
            file_storage=file_storage,
            image_downloader=image_downloader,
            config=config,
        )
        titles.extend(t)

    return TaskResult(
        summary=f"排行榜 ({mode}): 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


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
    config,
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
            t = await _fan_out_author_fetch(
                novels, force=force,
                client=client, uow=uow,
                file_storage=file_storage,
                image_downloader=image_downloader,
                config=config,
            )
            all_titles.extend(t)

    return TaskResult(
        summary=f"搜索 ({keyword}): 新增 {len(all_titles)} 本小说",
        new_novel_titles=all_titles,
    )
