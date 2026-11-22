"""Background task adapters for novel discovery and author removal (fetch lane)."""

from pydantic import BaseModel

from copixiv.core.models import TaskResult
from copixiv.features.authors.service import delete_author
from copixiv.features.novels.use_cases.discovery import (
    fetch_author_novels,
    fetch_follow_feed,
    fetch_ranking_novels,
    fetch_search_novels,
    fetch_single_novel,
    fetch_special_follow_novels,
    retry_failed_novels,
)
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


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


class FailedRetryArgs(BaseModel):
    novel_ids: list[int]


@register(
    "novel_fetch",
    description="抓取指定 ID 的小说",
    args=NovelFetchArgs,
    lane="fetch",
)
async def novel_fetch(args: NovelFetchArgs, ctx: TaskContext) -> TaskResult:
    """Download and persist a single novel by ID."""
    return await fetch_single_novel(
        args.id,
        redownload=args.redownload,
        session_factory=ctx.session_factory,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )


@register(
    "novel_follow",
    description="抓取指定pixiv账号中关注的作者的新作",
    args=NovelFollowArgs,
    lane="fetch",
)
async def novel_follow(args: NovelFollowArgs, ctx: TaskContext) -> TaskResult:
    """Fetch new novels from followed users."""
    return await fetch_follow_feed(
        days=args.days,
        force=args.force,
        session_factory=ctx.session_factory,
        client=ctx.require_client(),
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )


@register(
    "author_fetch",
    description="抓取指定作者的全部作品",
    args=AuthorFetchArgs,
    lane="fetch",
)
async def author_fetch(args: AuthorFetchArgs, ctx: TaskContext) -> TaskResult:
    """Fetch all novels by an author."""
    return await fetch_author_novels(
        args.author_id,
        force=args.force,
        redownload=args.redownload,
        uow=ctx.require_uow(),
        client=ctx.require_client(),
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
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
        client=ctx.require_client(),
    )


@register(
    "author_special_follow",
    description="抓取特别关注的作者的新作",
    lane="fetch",
)
async def author_special_follow(ctx: TaskContext) -> TaskResult:
    """Check for new novels from specially-followed authors."""
    return await fetch_special_follow_novels(
        uow=ctx.require_uow(),
        client=ctx.require_client(),
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )


@register(
    "novel_ranking",
    description="逐日抓取指定日期范围的日排行榜",
    args=NovelRankingArgs,
    lane="fetch",
)
async def novel_ranking(args: NovelRankingArgs, ctx: TaskContext) -> TaskResult:
    """Fetch novel rankings."""
    return await fetch_ranking_novels(
        mode=args.mode,
        days=args.days,
        force=args.force,
        client=ctx.require_client(),
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        child_uow_factory=ctx.child_uow,
    )


@register(
    "novel_search",
    description="搜索指定关键词与时间范围的小说",
    args=NovelSearchArgs,
    lane="fetch",
)
async def novel_search(args: NovelSearchArgs, ctx: TaskContext) -> TaskResult:
    """Search novels by keyword over a time range."""
    return await fetch_search_novels(
        keyword=args.keyword,
        months=args.months,
        minlike=args.minlike,
        force=args.force,
        client=ctx.require_client(),
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
        child_uow_factory=ctx.child_uow,
    )


@register(
    "failed_retry",
    description="重试「下载失败」清单中的小说",
    args=FailedRetryArgs,
    lane="fetch",
)
async def failed_retry(args: FailedRetryArgs, ctx: TaskContext) -> TaskResult:
    """Manually retry failed downloads from the 「下载失败」 ledger."""
    return await retry_failed_novels(
        args.novel_ids,
        session_factory=ctx.session_factory,
        client=ctx.client,
        file_storage=ctx.file_storage,
        image_downloader=ctx.image_downloader,
    )
