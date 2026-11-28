"""Novel discovery and Pixiv crawl orchestration use cases.

Contains all pure business orchestration previously embedded inside
``tasks/novels.py``, taking explicit runtime dependencies instead of
``TaskContext``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from copixiv.core.draft import safe_get
from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_PIXIV, TaskResult
from copixiv.core.sources import SourceCapability, SourceCredentialRecord, SourceRuntimeConfig
from copixiv.core.text import filter_chinese
from copixiv.core.time import month_ranges
from copixiv.db.base import ensure_parent_rows
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.ingest import BookChapterIngestor, ingest
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient
from copixiv.sources.local import LocalSourcePlugin
from copixiv.sources.pixiv import PixivSourcePlugin
from copixiv.sources.registry import SourceRegistry

# How many ``fetch_author_novels`` branches may run at once during ranking/search fan-out.
_FAN_OUT_LIMIT = 8
# How many novel IDs are processed per ``ingest`` batch in retry flows.
_RETRY_BATCH_SIZE = 50


def _ensure_pixiv_enabled(source_service: Any | None) -> None:
    if source_service is not None and not source_service.is_enabled(SOURCE_PIXIV):
        raise ValidationError("Pixiv 数据源已禁用")


def _resolve_registry(
    source_registry: Any | None,
    source_service: Any | None,
) -> SourceRegistry | None:
    if source_service is not None and getattr(source_service, "registry", None) is not None:
        return source_service.registry
    return source_registry


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


async def fetch_follow_feed(
    *,
    days: int = 3,
    force: bool = False,
    session_factory: Any,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Fetch new novels from followed users on Pixiv."""
    del source_registry
    _ensure_pixiv_enabled(source_service)
    if client is None:
        raise RuntimeError("TaskContext.client is not set")
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
    uow: SqlUnitOfWork,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Fetch all novels by an author."""
    del source_registry
    _ensure_pixiv_enabled(source_service)
    if client is None:
        raise RuntimeError("TaskContext.client is not set")

    def _plan(uw: SqlUnitOfWork) -> tuple[bool, dict | None]:
        repo = SQLAlchemyAuthorRepository(uw.session)
        skip = (not force) and repo.was_updated_today(author_id)
        return skip, (None if skip else repo.get_by_id(author_id))

    skip, author = await read_transaction(uow, _plan)
    if skip:
        logger.info(f"Skip Author {author_id}, already updated today.")
        return TaskResult(summary=f"作者 #{author_id} 今日已更新，跳过")

    if not author:
        async with client.account_rule(force_follow=True):
            await client.user_follow_add(author_id)

    resp = await client.user_novels(author_id, fetch_all=True)
    raw_novels = safe_get(resp, "novels", [])
    novels = filter_chinese(raw_novels)

    out = await ingest(
        novels,
        force=force,
        session_factory=uow.session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
    )

    name = (
        safe_get(resp, "user.name", "")
        or next((safe_get(n, "user.name", "") for n in raw_novels if safe_get(n, "user.name")), "")
        or (author.get("author_name") or "" if author else "")
    )

    def _finish_author_update(uw: SqlUnitOfWork) -> None:
        ensure_parent_rows(uw.session, author_ids={author_id})
        repo = SQLAlchemyAuthorRepository(uw.session)
        repo.update_last_update(author_id)
        if name and (author is None or author.get("author_name") != name):
            repo.update_author_name(author_id, name)

    await run_write_transaction(uow, _finish_author_update)

    label = name or f"#{author_id}"
    return TaskResult(
        summary=_batch_summary(f"作者 {label}", out),
        new_novel_titles=out.new_titles,
        failed_ids=out.failed_ids,
    )


async def fetch_special_follow_novels(
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None = None,
    file_storage: Any,
    image_downloader: Any,
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    authors_by_source = await read_transaction(
        uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).get_special_follow_authors_by_source(),
    )

    if not authors_by_source:
        logger.info("No special followed authors.")
        return TaskResult(summary="无特别关注的作者")

    registry = _resolve_registry(source_registry, source_service)
    pixiv_novels: list[Any] = []

    for src, keys in authors_by_source.items():
        if not keys:
            continue
        if source_service is not None and not source_service.is_enabled(src):
            logger.info(f"Special follow: skipping disabled source {src}")
            continue

        plugin = registry.get(src) if registry is not None else None
        if (plugin is None and registry is None and src == SOURCE_PIXIV and client is not None) or (
            isinstance(plugin, PixivSourcePlugin) and plugin._client is None and client is not None
        ):
            plugin = PixivSourcePlugin(client=client)

        if plugin is None or not plugin.manifest.has_capability(SourceCapability.AUTHOR_FEED):
            logger.info(f"Special follow: skipping unsupported source {src}")
            continue

        runtime: SourceRuntimeConfig
        creds: Sequence[SourceCredentialRecord]
        if source_service is not None:

            def _load_source_ctx(
                uw: SqlUnitOfWork,
                target_src: str = src,
            ) -> tuple[SourceRuntimeConfig, Sequence[SourceCredentialRecord]]:
                return (
                    source_service.get_runtime_config(target_src, session=uw.session),
                    source_service.get_credentials(target_src, session=uw.session),
                )

            runtime, creds = await read_transaction(uow, _load_source_ctx)
        else:
            runtime = SourceRuntimeConfig(source=src, enabled=True)
            creds = ()

        feed_items = await plugin.fetch_author_feed(keys, runtime, creds)
        if src == SOURCE_PIXIV and feed_items:
            pixiv_novels.extend(feed_items)

    out = await ingest(
        pixiv_novels,
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
    seen_authors: set[int] | None = None,
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> tuple[list[str], list[int]]:
    """Filter to Chinese novels and fan out ``fetch_author_novels`` concurrently."""
    cn = filter_chinese(novels)
    if not cn:
        return [], []

    authors = {safe_get(n, "user.id") for n in cn if safe_get(n, "user")} - {None}
    if seen_authors is not None:
        authors -= seen_authors
        seen_authors.update(authors)
    if not authors:
        return [], []

    async with client.account_rule():
        gate = asyncio.Semaphore(_FAN_OUT_LIMIT)

        async def _one(aid: int) -> TaskResult:
            async with gate:
                return await fetch_author_novels(
                    aid,
                    force=force,
                    uow=child_uow_factory(),
                    client=client,
                    file_storage=file_storage,
                    image_downloader=image_downloader,
                    source_registry=source_registry,
                    source_service=source_service,
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


def _fan_out_result(prefix: str, titles: list[str], failed_ids: list[int]) -> TaskResult:
    unique_failed = list(dict.fromkeys(failed_ids))
    summary = f"{prefix}: 新增 {len(titles)} 本小说"
    if unique_failed:
        summary += f"，失败 {len(unique_failed)} 本"
    return TaskResult(
        summary=summary,
        new_novel_titles=titles,
        failed_ids=unique_failed,
    )


async def fetch_ranking_novels(
    *,
    mode: str = "day_r18",
    days: int = 3,
    force: bool = False,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
    child_uow_factory: Callable[[], SqlUnitOfWork],
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Fetch novel rankings over the last *days* days."""
    _ensure_pixiv_enabled(source_service)
    if client is None:
        raise RuntimeError("TaskContext.client is not set")
    titles: list[str] = []
    failed_ids: list[int] = []
    seen_authors: set[int] = set()
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
            seen_authors=seen_authors,
            source_registry=source_registry,
            source_service=source_service,
        )
        titles.extend(t)
        failed_ids.extend(f)

    return _fan_out_result(f"排行榜 ({mode})", titles, failed_ids)


async def fetch_search_novels(
    *,
    keyword: str = "R-18",
    months: int = 1,
    minlike: int = 500,
    force: bool = False,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
    child_uow_factory: Callable[[], SqlUnitOfWork],
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Search novels by keyword over a time range and fan out author catalogues."""
    _ensure_pixiv_enabled(source_service)
    if client is None:
        raise RuntimeError("TaskContext.client is not set")
    end = datetime.now().astimezone() - timedelta(days=1)
    end_date = datetime(end.year, end.month, end.day)

    all_titles: list[str] = []
    failed_ids: list[int] = []
    seen_authors: set[int] = set()
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
                seen_authors=seen_authors,
                source_registry=source_registry,
                source_service=source_service,
            )
            all_titles.extend(t)
            failed_ids.extend(f)

    return _fan_out_result(f"搜索 ({keyword})", all_titles, failed_ids)


async def retry_failed_novels(
    novel_ids: list[int] | None = None,
    *,
    ids: list[int] | None = None,
    source: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    session_factory: Any,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
    source_registry: Any | None = None,
    source_service: Any | None = None,
) -> TaskResult:
    """Retry failed downloads from the 「下载失败」 ledger."""
    if novel_ids is not None:
        _ensure_pixiv_enabled(source_service)
        target_ids = list(dict.fromkeys(novel_ids))
        if not target_ids:
            return TaskResult(summary="重试列表为空", new_novel_titles=[])

        new_titles: list[str] = []
        failed_ids: list[int] = []
        for i in range(0, len(target_ids), _RETRY_BATCH_SIZE):
            chunk = target_ids[i : i + _RETRY_BATCH_SIZE]
            if on_progress is not None:
                done = min(i + _RETRY_BATCH_SIZE, len(target_ids))
                await on_progress(f"重试 Pixiv 小说 {done}/{len(target_ids)}…")
            out = await ingest(
                ids=chunk,
                force=True,
                session_factory=session_factory,
                client=client,
                file_storage=file_storage,
                image_downloader=image_downloader,
            )
            new_titles.extend(out.new_titles)
            failed_ids.extend(out.failed_ids)
        failed_ids = list(dict.fromkeys(failed_ids))
        ok = len(target_ids) - len(failed_ids)
        return TaskResult(
            summary=f"重试失败小说: 成功 {ok}/{len(target_ids)}",
            new_novel_titles=new_titles,
            failed_ids=failed_ids,
        )

    normalized_source = source.strip() if isinstance(source, str) and source.strip() else None
    failed_row_ids = list(dict.fromkeys(ids or []))
    uow = SqlUnitOfWork(session_factory)
    if failed_row_ids:
        row_tuples: list[tuple[int, str, str, int | None, dict[str, Any] | None]] = await read_transaction(
            uow,
            lambda uw: [
                (
                    r.id,
                    r.source,
                    r.source_key,
                    r.novel_id,
                    dict(r.extra) if r.extra is not None else None,
                )
                for r in FailedNovelRepository(uw.session).get_by_ids(failed_row_ids)
            ],
        )
        if not row_tuples:
            return TaskResult(summary="重试列表为空", new_novel_titles=[])
    else:
        row_tuples = await read_transaction(
            uow,
            lambda uw: [
                (
                    r.id,
                    r.source,
                    r.source_key,
                    r.novel_id,
                    dict(r.extra) if r.extra is not None else None,
                )
                for r in FailedNovelRepository(uw.session).get_retryable_rows(
                    source=normalized_source,
                    limit=limit,
                )
            ],
        )
        if not row_tuples:
            return TaskResult(summary="没有可重试的失败记录", new_novel_titles=[])

    registry = _resolve_registry(source_registry, source_service) or SourceRegistry(
        [PixivSourcePlugin(), LocalSourcePlugin()]
    )

    by_source: dict[str, list[tuple[int, str, int | None, dict[str, Any] | None]]] = {}
    for row_id, src, source_key, nid, extra in row_tuples:
        by_source.setdefault(src, []).append((row_id, source_key, nid, extra))

    pixiv_ids: list[int] = []
    external_batches: list[tuple[str, list[tuple[int, str, int | None, dict[str, Any] | None]]]] = []
    skipped = 0
    for src, items in by_source.items():
        if source_service is not None and not source_service.is_enabled(src):
            logger.warning(f"retry_failed_novels: 数据源 {src} 已禁用，跳过 {len(items)} 条失败记录")
            skipped += len(items)
            continue
        if not registry.has_capability(src, SourceCapability.RETRY_FAILURE):
            logger.warning(f"retry_failed_novels: 数据源 {src} 不支持重试，跳过 {len(items)} 条失败记录")
            skipped += len(items)
            continue
        if src == SOURCE_PIXIV:
            for _row_id, source_key, nid, _extra in items:
                try:
                    pixiv_ids.append(int(source_key))
                except (TypeError, ValueError):
                    if nid is not None:
                        pixiv_ids.append(int(nid))
                    else:
                        skipped += 1
        else:
            external_batches.append((src, items))

    pixiv_ids = list(dict.fromkeys(pixiv_ids))
    new_titles = []
    failed_ids = []
    for i in range(0, len(pixiv_ids), _RETRY_BATCH_SIZE):
        chunk = pixiv_ids[i : i + _RETRY_BATCH_SIZE]
        if on_progress is not None:
            done = min(i + _RETRY_BATCH_SIZE, len(pixiv_ids))
            await on_progress(f"重试 Pixiv 失败记录 {done}/{len(pixiv_ids)}…")
        out = await ingest(
            ids=chunk,
            force=True,
            session_factory=session_factory,
            client=client,
            file_storage=file_storage,
            image_downloader=image_downloader,
        )
        new_titles.extend(out.new_titles)
        failed_ids.extend(out.failed_ids)

    pixiv_failed_count = len(list(dict.fromkeys(failed_ids)))
    ext_attempted = 0
    ext_ok = 0

    if external_batches:
        ingestor = BookChapterIngestor(
            session_factory=session_factory,
            file_storage=file_storage,
            image_downloader=image_downloader,
            source_registry=registry,
            source_service=source_service,
        )
        for src, raw_rows in external_batches:
            ext_out = await ingestor.retry_failures(src, raw_rows, on_progress=on_progress)
            new_titles.extend(ext_out.new_titles)
            failed_ids.extend(ext_out.failed_ids)
            ext_attempted += len(raw_rows)
            src_failed = ext_out.failure_count
            ext_ok += max(len(raw_rows) - src_failed, 0)

    failed_ids = list(dict.fromkeys(failed_ids))
    total_attempted = len(pixiv_ids) + ext_attempted
    ok = (len(pixiv_ids) - pixiv_failed_count) + ext_ok
    if skipped and not total_attempted:
        summary = f"重试失败记录: 跳过 {skipped} 条不支持或已禁用来源"
    elif skipped:
        summary = f"重试失败小说: 成功 {ok}/{total_attempted}（跳过 {skipped} 条不支持或已禁用来源）"
    else:
        summary = f"重试失败小说: 成功 {ok}/{total_attempted}"
    return TaskResult(
        summary=summary,
        new_novel_titles=new_titles,
        failed_ids=failed_ids,
    )


__all__ = [
    "_batch_summary",
    "fan_out_author_fetch",
    "fetch_author_novels",
    "fetch_follow_feed",
    "fetch_ranking_novels",
    "fetch_search_novels",
    "fetch_special_follow_novels",
    "retry_failed_novels",
]
