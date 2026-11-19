"""Novel discovery and Pixiv crawl orchestration use cases.

Contains all pure business orchestration previously embedded inside
``tasks/novels.py``, taking explicit runtime dependencies instead of
``TaskContext``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from copixiv.core.draft import safe_get
from copixiv.core.models import TaskResult
from copixiv.core.text import filter_chinese
from copixiv.core.time import month_ranges
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.resolve_names import (
    collect_author_names,
    writeback_author_names,
)
from copixiv.features.novels.ingest import ingest
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient

# How many ``fetch_author_novels`` branches may run at once during ranking/search fan-out.
_FAN_OUT_LIMIT = 8


def _batch_summary(prefix: str, out: Any) -> str:
    """One-line result summary that does not hide partial failure."""
    new = out.new_count
    refreshed = max(len(out.titles) - new, 0)
    summary = f"{prefix}: 新增 {new} 本小说"
    if refreshed:
        summary += f"，更新 {refreshed} 本"
    failed = len(out.failed_ids)
    if failed:
        summary += f"，失败 {failed} 本"
    return summary


async def fetch_single_novel(
    novel_id: int,
    *,
    redownload: bool = True,
    session_factory: Any,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Download and persist a single novel by ID."""
    out = await ingest(
        ids=[novel_id],
        force=redownload,
        session_factory=session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
    )
    title = out.titles[0] if out.titles else None
    if out.failed:
        summary = f"小说 #{novel_id} 获取失败"
    elif out.new_count and title:
        summary = f"下载完成: {title}"
    elif out.rewritten_ids and title:
        summary = f"已更新: {title}"
    elif title is None:
        summary = f"小说 #{novel_id} 获取失败（未取到内容）"
    else:
        summary = "已存在，跳过"
    return TaskResult(
        summary=summary,
        new_novel_titles=(out.titles if out.new_count else []),
        failed_ids=([novel_id] if out.failed or title is None else []),
    )


async def fetch_follow_feed(
    *,
    days: int = 3,
    force: bool = False,
    session_factory: Any,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Fetch new novels from followed users on Pixiv."""
    fetch_til = datetime.now().astimezone() - timedelta(days=days)
    async with client.account_rule(force_follow=True):
        resp = await client.novel_follow(fetch_til=fetch_til)

    novels = filter_chinese(safe_get(resp, "novels", []))
    out = await ingest(
        novels,
        force=force,
        session_factory=session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
    )

    return TaskResult(
        summary=_batch_summary("关注更新", out),
        new_novel_titles=out.new_titles,
        failed_ids=out.failed_ids,
    )


async def fetch_author_novels(
    author_id: int,
    *,
    force: bool = False,
    redownload: bool = False,
    uow: SqlUnitOfWork,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Fetch all novels by an author."""

    def _plan(uw: SqlUnitOfWork) -> tuple[bool, dict | None]:
        repo = SQLAlchemyAuthorRepository(uw.session)
        skip = (not force) and repo.need_update(author_id)
        return skip, (None if skip else repo.get_by_id(author_id))

    skip, author = await read_transaction(uow, _plan)
    if skip:
        logger.info(f"Skip Author {author_id}, already updated today.")
        return TaskResult(summary=f"作者 #{author_id} 今日已更新，跳过")

    if not author:
        async with client.account_rule(force_follow=True):
            await client.user_follow_add(author_id)

    resp = await client.user_novels(author_id, fetch_all=True)
    novels = filter_chinese(safe_get(resp, "novels", []))

    out = await ingest(
        novels,
        force=redownload,
        session_factory=uow.session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
    )

    await run_write_transaction(
        uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).update_last_update(author_id),
    )

    mapping = await collect_author_names(
        {author_id},
        uow=uow,
        client=client,
    )
    await run_write_transaction(
        uow,
        lambda uw: writeback_author_names(mapping, uw),
    )
    name = mapping.get(author_id, "")

    label = name or f"#{author_id}"
    return TaskResult(
        summary=_batch_summary(f"作者 {label}", out),
        new_novel_titles=out.new_titles,
        failed_ids=out.failed_ids,
    )


async def fetch_special_follow_novels(
    *,
    uow: SqlUnitOfWork,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    author_ids = await read_transaction(
        uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).get_special_follow_author_ids(),
    )

    if not author_ids:
        logger.info("No special followed authors.")
        return TaskResult(summary="无特别关注的作者")

    responses = await asyncio.gather(
        *[client.user_novels(aid) for aid in author_ids],
        return_exceptions=True,
    )

    all_novels = []
    for author_id, resp in zip(author_ids, responses, strict=True):
        if isinstance(resp, Exception):
            logger.error(f"Failed to fetch novels for author {author_id}: {resp}")
            continue
        novels = safe_get(resp, "novels", [])
        if novels:
            all_novels.extend(novels)

    all_novels = filter_chinese(all_novels)

    out = await ingest(
        all_novels,
        session_factory=uow.session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
    )

    return TaskResult(
        summary=_batch_summary("特别关注", out),
        new_novel_titles=out.new_titles,
        failed_ids=out.failed_ids,
    )


async def fan_out_author_fetch(
    novels: list,
    *,
    force: bool,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
    child_uow_factory: Callable[[], SqlUnitOfWork],
) -> tuple[list[str], list[int]]:
    """Filter to Chinese novels and fan out ``fetch_author_novels`` concurrently."""
    cn = filter_chinese(novels)
    if not cn:
        return [], []

    authors = {safe_get(n, "user.id") for n in cn if safe_get(n, "user")} - {None}
    if not authors:
        return [], []

    async with client.account_rule():
        gate = asyncio.Semaphore(_FAN_OUT_LIMIT)

        async def _one(aid: int) -> TaskResult:
            async with gate:
                return await fetch_author_novels(
                    aid,
                    force=force,
                    redownload=False,
                    uow=child_uow_factory(),
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                )

        results = await asyncio.gather(
            *[_one(a) for a in authors],
            return_exceptions=True,
        )

    titles: list[str] = []
    failed_ids: list[int] = []
    for aid, r in zip(authors, results, strict=True):
        if isinstance(r, TaskResult):
            titles.extend(r.new_novel_titles)
            failed_ids.extend(r.failed_ids)
        else:
            logger.error(f"扇出 author_fetch 失败: 作者 #{aid}: {r!r}")
    return titles, failed_ids


async def fetch_ranking_novels(
    *,
    mode: str = "day_r18",
    days: int = 3,
    force: bool = False,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
    child_uow_factory: Callable[[], SqlUnitOfWork],
) -> TaskResult:
    """Fetch novel rankings over the last *days* days."""
    titles: list[str] = []
    failed_ids: list[int] = []
    for delta in range(1, days + 1):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(
            mode=mode,
            date=target,
            fetch_all=True,
        )
        novels = safe_get(resp, "novels", [])
        if not novels:
            continue
        t, f = await fan_out_author_fetch(
            novels,
            force=force,
            client=client,
            file_storage=file_storage,
            image_downloader=image_downloader,
            child_uow_factory=child_uow_factory,
        )
        titles.extend(t)
        failed_ids.extend(f)

    summary = f"排行榜 ({mode}): 新增 {len(titles)} 本小说"
    if failed_ids:
        summary += f"，失败 {len(set(failed_ids))} 本"
    return TaskResult(
        summary=summary,
        new_novel_titles=titles,
        failed_ids=list(dict.fromkeys(failed_ids)),
    )


async def fetch_search_novels(
    *,
    keyword: str = "R-18",
    months: int = 1,
    minlike: int = 500,
    force: bool = False,
    client: PixivClient,
    file_storage: Any,
    image_downloader: Any,
    child_uow_factory: Callable[[], SqlUnitOfWork],
) -> TaskResult:
    """Search novels by keyword over a time range and fan out author catalogues."""
    end = datetime.now().astimezone() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles: list[str] = []
    failed_ids: list[int] = []
    async with client.account_rule(need_premium=True):
        for start_date, slice_end in month_ranges(end_date, months):
            logger.info(f"Searching {start_date.date()} ~ {slice_end.date()}")
            resp = await client.search_novel(
                keyword,
                "keyword",
                "popular_desc",
                start_date=start_date,
                end_date=slice_end,
                fetch_minlike=minlike,
            )
            novels = safe_get(resp, "novels", [])
            if not novels:
                continue
            t, f = await fan_out_author_fetch(
                novels,
                force=force,
                client=client,
                file_storage=file_storage,
                image_downloader=image_downloader,
                child_uow_factory=child_uow_factory,
            )
            all_titles.extend(t)
            failed_ids.extend(f)

    summary = f"搜索 ({keyword}): 新增 {len(all_titles)} 本小说"
    if failed_ids:
        summary += f"，失败 {len(set(failed_ids))} 本"
    return TaskResult(
        summary=summary,
        new_novel_titles=all_titles,
        failed_ids=list(dict.fromkeys(failed_ids)),
    )


async def retry_failed_novels(
    novel_ids: list[int],
    *,
    session_factory: Any,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Retry failed downloads from the 「下载失败」 ledger."""
    ids = list(dict.fromkeys(novel_ids))
    if not ids:
        return TaskResult(summary="重试列表为空", new_novel_titles=[])

    results = await asyncio.gather(
        *[
            fetch_single_novel(
                nid,
                redownload=True,
                session_factory=session_factory,
                client=client,
                file_storage=file_storage,
                image_downloader=image_downloader,
            )
            for nid in ids
        ],
        return_exceptions=True,
    )

    titles: list[str] = []
    failed_ids: list[int] = []
    for nid, result in zip(ids, results, strict=True):
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


__all__ = [
    "_batch_summary",
    "fan_out_author_fetch",
    "fetch_author_novels",
    "fetch_follow_feed",
    "fetch_ranking_novels",
    "fetch_search_novels",
    "fetch_single_novel",
    "fetch_special_follow_novels",
    "retry_failed_novels",
]
