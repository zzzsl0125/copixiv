"""Background tasks for novel operations (business tasks on the kernel).

Each task is a self-contained module entry (docs/MODULARITY.md §M8):

- a Pydantic ``*Args`` model — the JSON parameter contract,
- a function taking ``(args, ctx)`` — dependencies arrive exclusively
  through :class:`~copixiv.tasks.context.TaskContext`,
- a declarative ``@register`` with name + args model.

All tasks return a :class:`TaskResult` so the manager / notifier knows
**what kind** of work was done — novel discovery vs. maintenance — and
can format notifications accordingly.
"""

import asyncio
from datetime import datetime, timedelta

from pydantic import BaseModel

from copixiv.application.author.resolve_names import resolve_author_names
from copixiv.application.novel.download_novel import DownloadNovelUseCase
from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.services.parsing import safe_get
from copixiv.infrastructure.database.write_lock import db_write
from copixiv.log import logger

from .context import TaskContext
from .pipeline import (
    _batch_handle,
    _filter_chinese_novels,
    _month_ranges,
)
from .registry import register


# ---------------------------------------------------------------------------
# Argument models (JSON contract per task)
# ---------------------------------------------------------------------------


class NovelFetchArgs(BaseModel):
    id: int
    redownload: bool = True


class NovelFollowArgs(BaseModel):
    days: int = 3
    force: bool = False


class AuthorFetchArgs(BaseModel):
    author_id: int
    force: bool = False
    redownload: bool = False


class AuthorDeleteArgs(BaseModel):
    author_id: int


class NovelRankingArgs(BaseModel):
    mode: str = "day_r18"
    days: int = 3
    force: bool = False


class NovelSearchArgs(BaseModel):
    keyword: str = "R-18"
    months: int = 1
    minlike: int = 500
    force: bool = False


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


async def _fan_out_author_fetch(
    novels: list,
    *,
    force: bool,
    ctx: TaskContext,
) -> list[str]:
    """Given a list of Pixiv API novel objects, filter to Chinese novels,
    extract unique author IDs, and fan out ``author_fetch`` concurrently.

    Used by :func:`novel_ranking` and :func:`novel_search`.

    Each concurrent ``author_fetch`` gets its own UnitOfWork (via
    ``ctx.child_uow()``) — sessions are never shared across coroutines.
    All database writes are serialized by the global ``db_write()`` lock
    inside the pipeline, so concurrency here only ever covers network I/O.

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

    async with ctx.client.account_rule():
        results = await asyncio.gather(*[
            author_fetch(
                AuthorFetchArgs(author_id=a, force=force),
                ctx.with_uow(ctx.child_uow()),
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


@register("novel_fetch", args=NovelFetchArgs)
async def novel_fetch(args: NovelFetchArgs, ctx: TaskContext) -> TaskResult:
    """Download and persist a single novel by ID."""
    use_case = DownloadNovelUseCase(
        client=ctx.client,
        uow=ctx.uow,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        write_lock=ctx.write_lock,
    )
    return await use_case.execute(args.id, redownload=args.redownload)


@register("novel_follow", args=NovelFollowArgs)
async def novel_follow(args: NovelFollowArgs, ctx: TaskContext) -> TaskResult:
    """Fetch new novels from followed users.

    Runs on the designated「追更账号」(``is_follow`` on the tokens table)
    because the Pixiv novel_follow endpoint returns results scoped to the
    authenticated account's own following list.

    Collect-then-persist: the feed is fetched without touching the
    database, then processed by :func:`_batch_handle`.
    """
    fetch_til = datetime.now().astimezone() - timedelta(days=args.days)
    async with ctx.client.account_rule(
        force_follow=True,
    ):
        resp = await ctx.client.novel_follow(fetch_til=fetch_til)

    novels = _filter_chinese_novels(safe_get(resp, "novels", []))
    titles, new_author_ids = await _batch_handle(
        novels, ctx.session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader, redownload=args.force,
    )

    if new_author_ids:
        await resolve_author_names(
            new_author_ids,
            client=ctx.client, uow=ctx.uow, write_lock=ctx.write_lock,
        )

    return TaskResult(
        summary=f"关注更新: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


@register("author_fetch", args=AuthorFetchArgs)
async def author_fetch(args: AuthorFetchArgs, ctx: TaskContext) -> TaskResult:
    """Fetch all novels by an author.

    Collect-then-persist flow: the whole catalogue is fetched without
    touching the database (client accumulates pages into ``resp.novels``),
    then :func:`_batch_handle` downloads new novels concurrently and
    persists everything in a single write transaction inside
    ``db_write()``.  No transaction is ever held across network I/O, and
    no write happens outside the global write lock.
    """
    session_factory = ctx.session_factory
    uow = ctx.uow

    # Plan — read-only, short transaction, no lock.
    async with uow.begin():
        if not args.force and not await uow.authors.need_update(args.author_id):
            logger.info(
                f"Skip Author {args.author_id}, already updated today."
            )
            return TaskResult(
                summary=f"作者 #{args.author_id} 今日已更新，跳过"
            )

        author = await uow.authors.get_by_id(args.author_id)

    if not author:
        async with ctx.client.account_rule(
            force_follow=True,
        ):
            await ctx.client.user_follow_add(args.author_id)

    # Collect — pure network, pages accumulate into resp.novels.
    resp = await ctx.client.user_novels(args.author_id, fetch_all=True)
    novels = _filter_chinese_novels(safe_get(resp, "novels", []))

    # Persist — plan → download → write, writes serialized by db_write().
    titles, _new_author_ids = await _batch_handle(
        novels, session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader, redownload=args.redownload,
    )

    # Mark the author as updated today (same write discipline).
    async with db_write():
        async with uow.begin():
            await uow.authors.update_last_update(args.author_id)

    # Resolve author name — webview API doesn't return it.
    resolved = await resolve_author_names(
        {args.author_id},
        client=ctx.client, uow=uow, write_lock=ctx.write_lock,
    )
    name = resolved.get(args.author_id, "")

    label = name or f"#{args.author_id}"
    return TaskResult(
        summary=f"作者 {label}: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


@register("author_delete", args=AuthorDeleteArgs)
async def author_delete(args: AuthorDeleteArgs, ctx: TaskContext) -> TaskResult:
    """Delete an author and all their novels."""
    async with db_write():
        async with ctx.uow.begin():
            await ctx.uow.authors.delete_author_and_data(args.author_id)

    async with ctx.client.account_rule(
        force_follow=True
    ):
        await ctx.client.user_follow_delete(args.author_id)

    return TaskResult(summary=f"已删除作者 #{args.author_id} 及其全部小说")


@register("author_special_follow")
async def author_special_follow(ctx: TaskContext) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    async with ctx.uow.begin():
        author_ids = await ctx.uow.authors.get_special_follow_author_ids()

    if not author_ids:
        logger.info("No special followed authors.")
        return TaskResult(summary="无特别关注的作者")

    # Fan out: fetch novels from all authors concurrently.
    # Each call goes through the client semaphore (max 5) and picks
    # the LRU account, so up to 5 accounts call the API in parallel.
    responses = await asyncio.gather(
        *[ctx.client.user_novels(aid) for aid in author_ids],
        return_exceptions=True,
    )

    all_novels = []
    for author_id, resp in zip(author_ids, responses):
        if isinstance(resp, Exception):
            logger.error(
                f"Failed to fetch novels for author {author_id}: {resp}"
            )
            continue
        novels = safe_get(resp, "novels", [])
        if novels:
            all_novels.extend(novels)

    titles, new_author_ids = await _batch_handle(
        all_novels, ctx.session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )

    if new_author_ids:
        await resolve_author_names(
            new_author_ids,
            client=ctx.client, uow=ctx.uow, write_lock=ctx.write_lock,
        )

    return TaskResult(
        summary=f"特别关注: 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


@register("novel_ranking", args=NovelRankingArgs)
async def novel_ranking(args: NovelRankingArgs, ctx: TaskContext) -> TaskResult:
    """Fetch novel rankings."""
    titles: list[str] = []
    for delta in range(1, args.days + 1):  # days=3 → 昨天/前天/大前天（当天榜单不全，跳过）
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await ctx.client.novel_ranking(
            mode=args.mode, date=target, fetch_all=True,
        )
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        t = await _fan_out_author_fetch(novels, force=args.force, ctx=ctx)
        titles.extend(t)

    return TaskResult(
        summary=f"排行榜 ({args.mode}): 新增 {len(titles)} 本小说",
        new_novel_titles=titles,
    )


@register("novel_search", args=NovelSearchArgs)
async def novel_search(args: NovelSearchArgs, ctx: TaskContext) -> TaskResult:
    """Search novels by keyword over a time range.

    Pagination accumulates results in ``resp.novels`` (no handler needed).
    For each Chinese novel found, the author's full catalogue is fetched
    via :func:`author_fetch` — same pattern as :func:`novel_ranking`.
    """
    end = datetime.now().astimezone() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles: list[str] = []
    async with ctx.client.account_rule(need_premium=True):
        for start_date, end_date in _month_ranges(end_date, args.months):
            logger.info(f"Searching {start_date.date()} ~ {end_date.date()}")
            resp = await ctx.client.search_novel(
                args.keyword, "keyword", "popular_desc",
                start_date=start_date, end_date=end_date,
                fetch_minlike=args.minlike,
            )
            novels = safe_get(resp, "novels", [])
            if not novels:
                continue
            t = await _fan_out_author_fetch(novels, force=args.force, ctx=ctx)
            all_titles.extend(t)

    return TaskResult(
        summary=f"搜索 ({args.keyword}): 新增 {len(all_titles)} 本小说",
        new_novel_titles=all_titles,
    )


class FailedRetryArgs(BaseModel):
    novel_ids: list[int]


@register("failed_retry", args=FailedRetryArgs)
async def failed_retry(args: FailedRetryArgs, ctx: TaskContext) -> TaskResult:
    """Manually retry failed downloads from the 「下载失败」 ledger.

    Fans out :func:`novel_fetch` (force re-download) for every id, each
    with its own child UoW — same pattern as :func:`_fan_out_author_fetch`.
    Successful retries clear their failure records (novel_fetch → persist
    forgets them); failures re-record with an incremented counter.
    """
    ids = list(dict.fromkeys(args.novel_ids))
    if not ids:
        return TaskResult(summary="重试列表为空", new_novel_titles=[])

    results = await asyncio.gather(
        *[
            novel_fetch(
                NovelFetchArgs(id=nid, redownload=True),
                ctx.with_uow(ctx.child_uow()),
            )
            for nid in ids
        ],
        return_exceptions=True,
    )

    titles: list[str] = []
    failed = 0
    for nid, result in zip(ids, results):
        if isinstance(result, TaskResult) and "获取失败" not in result.summary:
            titles.extend(result.new_novel_titles)
        else:
            failed += 1
            logger.error(f"failed_retry: #{nid} 重试失败: {result!r}")

    ok = len(ids) - failed
    return TaskResult(
        summary=f"重试失败小说: 成功 {ok}/{len(ids)}",
        new_novel_titles=titles,
    )
