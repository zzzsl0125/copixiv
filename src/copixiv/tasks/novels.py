"""Background task adapters for novel discovery and author removal (fetch lane)."""

import asyncio

from pydantic import BaseModel, Field, field_validator

from copixiv.core.models import SOURCE_PIXIV, TaskResult
from copixiv.features.authors.service import delete_author
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.use_cases.discovery import (
    fetch_author_novels,
    fetch_follow_feed,
    fetch_ranking_novels,
    fetch_search_novels,
    fetch_special_follow_novels,
    retry_failed_novels,
)
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class NovelFollowArgs(BaseModel):
    days: int = 3
    force: bool = False


class AuthorFetchArgs(BaseModel):
    author_id: int
    force: bool = False


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


class FailedRetryArgs(BaseModel):
    ids: list[int] = Field(
        default_factory=list,
        description="指定失败记录 ID 列表（留空时自动拉取台账中未达重试上限的记录）",
    )
    novel_ids: list[int] = Field(
        default_factory=list,
        description="兼容参数：指定 Pixiv 小说 ID 列表（留空时忽略）",
    )
    source: str | None = Field(
        default=None,
        description="仅重试指定数据源（如 pixiv / esj；留空表示全部已启用源；自动拉取模式生效）",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="自动拉取模式下本轮最多重试条数（留空表示不限）",
    )

    @field_validator("source", mode="before")
    @classmethod
    def _blank_source_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("limit", mode="before")
    @classmethod
    def _blank_limit_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


async def _failed_retry_timeout_sources(
    args: FailedRetryArgs,
    ctx: TaskContext,
) -> tuple[str, ...]:
    if args.novel_ids:
        return (SOURCE_PIXIV,)
    failed_ids = list(dict.fromkeys(args.ids or ()))
    if failed_ids:
        if ctx.session_factory is None:
            return ()

        def _read_selected_sources() -> tuple[str, ...]:
            with ctx.session_factory() as session:
                rows = FailedNovelRepository(session).get_by_ids(failed_ids)
                return tuple(dict.fromkeys(r.source for r in rows if r.source))

        return await asyncio.to_thread(_read_selected_sources)

    if args.source:
        return (args.source,)
    if ctx.session_factory is None:
        return ()

    def _read_retryable_sources() -> tuple[str, ...]:
        with ctx.session_factory() as session:
            rows = FailedNovelRepository(session).get_retryable_rows(limit=args.limit)
            return tuple(dict.fromkeys(r.source for r in rows if r.source))

    return await asyncio.to_thread(_read_retryable_sources)


@register(
    "novel_follow",
    description="抓取指定pixiv账号中关注的作者的新作",
    args=NovelFollowArgs,
    lane="fetch",
    source=SOURCE_PIXIV,
)
async def novel_follow(args: NovelFollowArgs, ctx: TaskContext) -> TaskResult:
    """Fetch new novels from followed users."""
    return await fetch_follow_feed(
        days=args.days,
        force=args.force,
        session_factory=ctx.session_factory,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "author_fetch",
    description="抓取指定作者的全部作品",
    args=AuthorFetchArgs,
    lane="fetch",
    source=SOURCE_PIXIV,
)
async def author_fetch(args: AuthorFetchArgs, ctx: TaskContext) -> TaskResult:
    """Fetch all novels by an author."""
    return await fetch_author_novels(
        args.author_id,
        force=args.force,
        uow=ctx.require_uow(),
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "author_delete",
    description="删除指定作者及其全部小说",
    args=AuthorDeleteArgs,
    lane="fetch",
)
async def author_delete(args: AuthorDeleteArgs, ctx: TaskContext) -> TaskResult:
    """Delete an author and all their novels."""
    return await delete_author(
        args.author_id,
        uow=ctx.require_uow(),
        file_storage=ctx.require_file_storage(),
        client=ctx.client,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "author_special_follow",
    description="抓取特别关注的作者的新作",
    lane="fetch",
    source=SOURCE_PIXIV,
)
async def author_special_follow(ctx: TaskContext) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    return await fetch_special_follow_novels(
        uow=ctx.require_uow(),
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "novel_ranking",
    description="逐日抓取指定日期范围的日排行榜",
    args=NovelRankingArgs,
    lane="fetch",
    source=SOURCE_PIXIV,
)
async def novel_ranking(args: NovelRankingArgs, ctx: TaskContext) -> TaskResult:
    """Fetch novel rankings."""
    return await fetch_ranking_novels(
        mode=args.mode,
        days=args.days,
        force=args.force,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        child_uow_factory=ctx.child_uow,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "novel_search",
    description="搜索指定关键词与时间范围的小说",
    args=NovelSearchArgs,
    lane="fetch",
    source=SOURCE_PIXIV,
)
async def novel_search(args: NovelSearchArgs, ctx: TaskContext) -> TaskResult:
    """Search novels by keyword over a time range."""
    return await fetch_search_novels(
        keyword=args.keyword,
        months=args.months,
        minlike=args.minlike,
        force=args.force,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        child_uow_factory=ctx.child_uow,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )


@register(
    "failed_retry",
    description="重试「下载失败」清单中的小说（留空可定时自动重试）",
    args=FailedRetryArgs,
    lane="fetch",
    timeout_sources=_failed_retry_timeout_sources,
)
async def failed_retry(args: FailedRetryArgs, ctx: TaskContext) -> TaskResult:
    """Manually or automatically retry failed downloads from the 「下载失败」 ledger."""
    return await retry_failed_novels(
        args.novel_ids or None,
        ids=args.ids or None,
        source=args.source,
        limit=args.limit,
        on_progress=ctx.report_progress,
        session_factory=ctx.session_factory,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        source_registry=ctx.source_registry,
        source_service=ctx.source_service,
    )
