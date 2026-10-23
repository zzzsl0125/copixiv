"""Background tasks for novel operations (business tasks on the kernel).

Each task is a self-contained module entry (docs/MODULARITY.md §M8):

- a Pydantic ``*Args`` model — the JSON parameter contract,
- a function taking ``(args, ctx)`` — dependencies arrive exclusively
  through :class:`~copixiv.tasks.kernel.TaskContext`,
- a declarative ``@register`` with name + args model.

All tasks return a :class:`TaskResult` so the manager / notifier knows
**what kind** of work was done — novel discovery vs. maintenance — and
can format notifications accordingly.
"""

import asyncio
from datetime import datetime, timedelta

from pydantic import BaseModel

from copixiv.features.authors.resolve_names import collect_author_names, writeback_author_names
from copixiv.features.novels.ingest import ingest, _filter_chinese_novels, _month_ranges
from copixiv.core.models import TaskResult
from copixiv.core.services import safe_get
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.log import logger

from .kernel import TaskContext
from .kernel import register


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


# How many ``author_fetch`` branches may run at once during a ranking/search
# fan-out.  Each branch opens its own session/transaction, and the engine pool
# is ``pool_size=6 + max_overflow=12``; a ranking page can name 20+ authors, so
# without a gate the persist phase alone could exhaust the pool and turn a
# transient checkout wait into a task failure.
_FAN_OUT_LIMIT = 8


async def _fan_out_author_fetch(
    novels: list,
    *,
    force: bool,
    ctx: TaskContext,
) -> tuple[list[str], list[int]]:
    """Given a list of Pixiv API novel objects, filter to Chinese novels,
    extract unique author IDs, and fan out ``author_fetch`` concurrently.

    Used by :func:`novel_ranking` and :func:`novel_search`.

    Each concurrent ``author_fetch`` gets its own UnitOfWork (via
    ``ctx.child_uow()``) — sessions are never shared across coroutines.
    There is no global ``db_write()`` lock: concurrency covers network I/O
    *and* short database write transactions, with LockNotAvailable retried
    on a fresh session (``run_write_transaction``).

    Returns ``(titles, failed_ids)``.  The failures of each child
    ``author_fetch`` must travel up: a ranking day where one of twenty authors
    failed still reports "success" otherwise, and the operator has no way to
    tell a quiet day from a broken one.
    """
    cn = _filter_chinese_novels(novels)
    if not cn:
        return [], []

    authors = {
        safe_get(n, "user.id")
        for n in cn if safe_get(n, "user")
    }
    if not authors:
        return [], []

    async with ctx.client.account_rule():
        gate = asyncio.Semaphore(_FAN_OUT_LIMIT)

        async def _one(author_id: int):
            async with gate:
                return await author_fetch(
                    AuthorFetchArgs(author_id=author_id, force=force),
                    ctx.with_uow(ctx.child_uow()),
                )

        results = await asyncio.gather(*[_one(a) for a in authors])

    # Each author_fetch returns a TaskResult; flatten titles and failures.
    titles: list[str] = []
    failed_ids: list[int] = []
    for r in results:
        if isinstance(r, TaskResult):
            titles.extend(r.new_novel_titles)
            failed_ids.extend(r.failed_ids)
    return titles, failed_ids


# ---------------------------------------------------------------------------
# Registered tasks
# ---------------------------------------------------------------------------


def _batch_summary(prefix: str, out) -> str:
    """One-line result summary that does not hide partial failure.

    ``f"{prefix}: 新增 {n} 本小说"`` is the wording the UI and the Telegram
    notifier have always shown, and it reads as "all good" even when half the
    batch failed.  The counts are now appended whenever something failed;
    ``failed_ids`` carries the machine-readable list for anyone who needs the
    ids rather than a count.
    """
    failed = len(out.failed_ids)
    summary = f"{prefix}: 新增 {len(out.titles)} 本小说"
    if failed:
        summary += f"，失败 {failed} 本"
    return summary


@register("novel_fetch", description="抓取指定 ID 的小说", args=NovelFetchArgs)
async def novel_fetch(args: NovelFetchArgs, ctx: TaskContext) -> TaskResult:
    """Download and persist a single novel by ID."""
    out = await ingest(
        ids=[args.id],
        force=args.redownload,
        session_factory=ctx.session_factory,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )
    if out.failed:
        summary = f"小说 #{args.id} 获取失败"
    elif out.titles and out.new_count:
        summary = f"下载完成: {out.titles[0]}"
    else:
        summary = "已存在，跳过"
    return TaskResult(
        summary=summary,
        new_novel_titles=out.titles,
        # Machine-readable outcome: callers must not infer success/failure from
        # the summary text (see TaskResult.failed_ids).
        failed_ids=([args.id] if out.failed else []),
    )


@register(
    "novel_follow",
    description="抓取指定pixiv账号中关注的作者的新作",
    args=NovelFollowArgs,
)
async def novel_follow(args: NovelFollowArgs, ctx: TaskContext) -> TaskResult:
    """Fetch new novels from followed users.

    Runs on the designated「追更账号」(``is_follow`` on the tokens table)
    because the Pixiv novel_follow endpoint returns results scoped to the
    authenticated account's own following list.

    Collect-then-persist: the feed is fetched without touching the
    database, then processed by :func:`ingest`.
    """
    fetch_til = datetime.now().astimezone() - timedelta(days=args.days)
    async with ctx.client.account_rule(
        force_follow=True,
    ):
        resp = await ctx.client.novel_follow(fetch_til=fetch_til)

    novels = _filter_chinese_novels(safe_get(resp, "novels", []))
    out = await ingest(
        novels, force=args.force,
        session_factory=ctx.session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )

    return TaskResult(
        summary=_batch_summary("关注更新", out),
        new_novel_titles=out.titles,
        failed_ids=out.failed_ids,
    )


@register("author_fetch", description="抓取指定作者的全部作品", args=AuthorFetchArgs)
async def author_fetch(args: AuthorFetchArgs, ctx: TaskContext) -> TaskResult:
    """Fetch all novels by an author.

    Collect-then-persist flow: the whole catalogue is fetched without
    touching the database (client accumulates pages into ``resp.novels``),
    then :func:`ingest` downloads new novels concurrently and persists
    everything in one short write transaction.  No transaction is ever
    held across network I/O, and there is no global write lock — brief
    LockNotAvailable conflicts are retried (see ``run_write_transaction``).
    """
    session_factory = ctx.session_factory
    uow = ctx.uow

    # Plan — read-only, short transaction, no lock.
    async with uow.begin():
        if not args.force and not await SQLAlchemyAuthorRepository(uow.session).need_update(args.author_id):
            logger.info(
                f"Skip Author {args.author_id}, already updated today."
            )
            return TaskResult(
                summary=f"作者 #{args.author_id} 今日已更新，跳过"
            )

        author = await SQLAlchemyAuthorRepository(uow.session).get_by_id(args.author_id)

    if not author:
        async with ctx.client.account_rule(
            force_follow=True,
        ):
            await ctx.client.user_follow_add(args.author_id)

    # Collect — pure network, pages accumulate into resp.novels.
    resp = await ctx.client.user_novels(args.author_id, fetch_all=True)
    novels = _filter_chinese_novels(safe_get(resp, "novels", []))

    # Persist — plan → download → write, each write is a short retried
    # transaction (no global lock).
    out = await ingest(
        novels, force=args.redownload,
        session_factory=session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )

    # Mark the author as updated today (same write discipline).
    await run_write_transaction(
        uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).update_last_update(
            args.author_id
        ),
    )

    # Resolve author name — webview API doesn't return it.
    mapping = await collect_author_names(
        {args.author_id}, uow=uow, client=ctx.client,
    )
    await run_write_transaction(
        uow, lambda uw: writeback_author_names(mapping, uw),
    )
    name = mapping.get(args.author_id, "")

    label = name or f"#{args.author_id}"
    return TaskResult(
        summary=_batch_summary(f"作者 {label}", out),
        new_novel_titles=out.titles,
        failed_ids=out.failed_ids,
    )


@register(
    "author_delete",
    description="删除指定作者及其全部小说",
    args=AuthorDeleteArgs,
)
async def author_delete(args: AuthorDeleteArgs, ctx: TaskContext) -> TaskResult:
    """Delete an author and all their novels."""
    await run_write_transaction(
        ctx.uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).delete_author_and_data(
            args.author_id
        ),
    )

    async with ctx.client.account_rule(
        force_follow=True
    ):
        await ctx.client.user_follow_delete(args.author_id)

    return TaskResult(summary=f"已删除作者 #{args.author_id} 及其全部小说")


@register("author_special_follow", description="抓取特别关注的作者的新作")
async def author_special_follow(ctx: TaskContext) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    async with ctx.uow.begin():
        author_ids = await SQLAlchemyAuthorRepository(ctx.uow.session).get_special_follow_author_ids()

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

    # Same language rule every other fetching task applies.  Without it this
    # task was the one path that imported whatever the followed authors
    # published — Japanese works included — into a Chinese-only library
    # (1,528 non-Chinese rows already in production, from before this task
    # existed in its current form).
    all_novels = _filter_chinese_novels(all_novels)

    out = await ingest(
        all_novels, session_factory=ctx.session_factory,
        client=ctx.client, file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )

    return TaskResult(
        summary=_batch_summary("特别关注", out),
        new_novel_titles=out.titles,
        failed_ids=out.failed_ids,
    )


@register(
    "novel_ranking",
    description="逐日抓取指定日期范围的日排行榜",
    args=NovelRankingArgs,
)
async def novel_ranking(args: NovelRankingArgs, ctx: TaskContext) -> TaskResult:
    """Fetch novel rankings."""
    titles: list[str] = []
    failed_ids: list[int] = []
    for delta in range(1, args.days + 1):  # days=3 → 昨天/前天/大前天（当天榜单不全，跳过）
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await ctx.client.novel_ranking(
            mode=args.mode, date=target, fetch_all=True,
        )
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        t, f = await _fan_out_author_fetch(novels, force=args.force, ctx=ctx)
        titles.extend(t)
        failed_ids.extend(f)

    summary = f"排行榜 ({args.mode}): 新增 {len(titles)} 本小说"
    if failed_ids:
        summary += f"，失败 {len(set(failed_ids))} 本"
    return TaskResult(
        summary=summary,
        new_novel_titles=titles,
        failed_ids=list(dict.fromkeys(failed_ids)),
    )


@register(
    "novel_search",
    description="搜索指定关键词与时间范围的小说",
    args=NovelSearchArgs,
)
async def novel_search(args: NovelSearchArgs, ctx: TaskContext) -> TaskResult:
    """Search novels by keyword over a time range.

    Pagination accumulates results in ``resp.novels`` (no handler needed).
    For each Chinese novel found, the author's full catalogue is fetched
    via :func:`author_fetch` — same pattern as :func:`novel_ranking`.
    """
    end = datetime.now().astimezone() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles: list[str] = []
    failed_ids: list[int] = []
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
            t, f = await _fan_out_author_fetch(
                novels, force=args.force, ctx=ctx,
            )
            all_titles.extend(t)
            failed_ids.extend(f)

    summary = f"搜索 ({args.keyword}): 新增 {len(all_titles)} 本小说"
    if failed_ids:
        summary += f"，失败 {len(set(failed_ids))} 本"
    return TaskResult(
        summary=summary,
        new_novel_titles=all_titles,
        failed_ids=list(dict.fromkeys(failed_ids)),
    )


class FailedRetryArgs(BaseModel):
    novel_ids: list[int]


@register(
    "failed_retry",
    description="重试「下载失败」清单中的小说",
    args=FailedRetryArgs,
)
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
    failed_ids: list[int] = []
    for nid, result in zip(ids, results):
        # Per-item outcome comes from the child's structured ``failed_ids``,
        # never from its human-readable summary.
        if isinstance(result, TaskResult) and not result.failed_ids:
            titles.extend(result.new_novel_titles)
        else:
            failed_ids.append(nid)
            logger.error(f"failed_retry: #{nid} 重试失败: {result!r}")

    ok = len(ids) - len(failed_ids)
    return TaskResult(
        summary=f"重试失败小说: 成功 {ok}/{len(ids)}",
        new_novel_titles=titles,
        failed_ids=failed_ids,
    )
