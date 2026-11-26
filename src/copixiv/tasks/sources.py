"""Background task adapter for cross-source book/chapter fetch by URL or ID ref."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_PIXIV, TaskResult
from copixiv.core.sources import ParsedSourceRef, SourceCapability
from copixiv.features.novels.ingest import BookChapterIngestor, BookIngestOutcome, ingest
from copixiv.log import logger
from copixiv.sources.esj.client import EsjChallengeError
from copixiv.sources.local import LocalSourcePlugin
from copixiv.sources.pixiv import PixivSourcePlugin
from copixiv.sources.registry import SourceRegistry
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.registry import register


class SourceFetchArgs(BaseModel):
    """Arguments for the ``source_fetch`` background task."""

    source: str | None = None
    refs: list[str] = Field(default_factory=list)
    force: bool = False


def _resolve_registry(ctx: TaskContext) -> SourceRegistry:
    if ctx.source_service is not None and getattr(ctx.source_service, "registry", None) is not None:
        return ctx.source_service.registry
    if ctx.source_registry is not None:
        return ctx.source_registry
    return SourceRegistry([PixivSourcePlugin(), LocalSourcePlugin()])


def _validate_source(
    source: str,
    registry: SourceRegistry,
    ctx: TaskContext,
) -> None:
    if ctx.source_service is not None:
        plugin = ctx.source_service.require_enabled(source)
        if not plugin.manifest.has_capability(SourceCapability.FETCH_BY_REF):
            raise ValidationError(f"数据源 {plugin.manifest.display_name} 不支持按引用抓取")
        return
    if not registry.has_capability(source, SourceCapability.FETCH_BY_REF):
        raise ValidationError(f"数据源 {source} 未注册或不支持按引用抓取")


def _format_summary(
    *,
    new_count: int,
    refreshed_count: int,
    skipped_existing: int,
    skipped_external: int,
    failed_count: int,
    invalid_count: int,
) -> str:
    parts = [f"新增 {new_count} 本小说"]
    if refreshed_count:
        parts.append(f"更新 {refreshed_count} 本")
    if skipped_existing:
        parts.append(f"跳过已有 {skipped_existing} 章")
    if skipped_external:
        parts.append(f"跳过外链 {skipped_external} 条")
    if failed_count:
        parts.append(f"失败 {failed_count} 条")
    if invalid_count:
        parts.append(f"无效引用 {invalid_count} 条")
    return "按引用抓取: " + "，".join(parts)


@register(
    "source_fetch",
    description="从指定或自动识别的数据源抓取书本/章节",
    args=SourceFetchArgs,
    lane="fetch",
)
async def source_fetch(args: SourceFetchArgs, ctx: TaskContext) -> TaskResult:
    """Parse refs across sources and fetch books/chapters via the matching ingestor."""
    raw_refs = [r.strip() for r in args.refs if isinstance(r, str) and r.strip()]
    if not raw_refs:
        return TaskResult(summary="按引用抓取: 未提供任何有效引用")

    registry = _resolve_registry(ctx)
    invalid_refs: list[str] = []
    parsed_refs: list[ParsedSourceRef] = []
    seen_refs: set[tuple[str, str, str, str | None]] = set()

    for raw in raw_refs:
        parsed = registry.parse_ref(raw, preferred_source=args.source)
        if parsed is None:
            logger.warning(f"source_fetch: 无法解析引用 {raw!r}")
            invalid_refs.append(raw)
            continue
        key = (parsed.source, parsed.kind, parsed.series_key, parsed.chapter_key)
        if key not in seen_refs:
            seen_refs.add(key)
            parsed_refs.append(parsed)

    for src in dict.fromkeys(p.source for p in parsed_refs):
        _validate_source(src, registry, ctx)

    if not parsed_refs:
        return TaskResult(
            summary=f"按引用抓取: 全部 {len(invalid_refs)} 条引用无法识别",
        )

    pixiv_ids: list[int] = []
    external_refs: list[ParsedSourceRef] = []
    for parsed in parsed_refs:
        if parsed.source == SOURCE_PIXIV:
            raw_id = parsed.chapter_key or parsed.series_key
            try:
                nid = int(raw_id)
            except (TypeError, ValueError):
                invalid_refs.append(parsed.normalized_ref or raw_id)
                continue
            if nid > 0:
                pixiv_ids.append(nid)
            else:
                invalid_refs.append(parsed.normalized_ref or raw_id)
        else:
            external_refs.append(parsed)

    new_novel_titles: list[str] = []
    failed_ids: list[int] = []
    new_count = 0
    fetched_count = 0
    failed_count = 0
    skipped_external = 0
    skipped_existing = 0

    if pixiv_ids:
        pixiv_ids = list(dict.fromkeys(pixiv_ids))
        pixiv_out = await ingest(
            ids=pixiv_ids,
            force=args.force,
            session_factory=ctx.session_factory,
            client=ctx.client,
            file_storage=ctx.file_storage,
            image_downloader=ctx.image_downloader,
        )
        new_novel_titles.extend(pixiv_out.new_titles)
        failed_ids.extend(pixiv_out.failed_ids)
        new_count += pixiv_out.new_count
        fetched_count += len(pixiv_out.titles)
        failed_count += len(pixiv_out.failed_ids)

    if external_refs:
        ingestor = BookChapterIngestor(
            session_factory=ctx.session_factory,
            file_storage=ctx.file_storage,
            image_downloader=ctx.image_downloader,
            source_registry=registry,
            source_service=ctx.source_service,
        )
        for parsed in external_refs:
            try:
                if parsed.kind == "book":
                    ext_out = await ingestor.ingest_book(
                        parsed.source,
                        parsed.series_key,
                        force=args.force,
                    )
                else:
                    ext_out = await ingestor.ingest_chapter(
                        parsed.source,
                        parsed.series_key,
                        parsed.chapter_key or "",
                        force=args.force,
                    )
            except asyncio.CancelledError:
                raise
            except (EsjChallengeError, ValidationError):
                raise
            except Exception as exc:
                logger.warning(f"source_fetch: 抓取 {parsed.normalized_ref or parsed.series_key} 失败: {exc}")
                failed_count += 1
                continue

            new_novel_titles.extend(ext_out.new_titles)
            failed_ids.extend(ext_out.failed_ids)
            new_count += ext_out.new_count
            fetched_count += len(ext_out.titles)
            if isinstance(ext_out, BookIngestOutcome):
                failed_count += ext_out.failure_count
                skipped_external += ext_out.skipped_external
                skipped_existing += ext_out.skipped_existing
            else:
                failed_count += len(ext_out.failed_ids)

    refreshed_count = max(fetched_count - new_count, 0)
    summary = _format_summary(
        new_count=new_count,
        refreshed_count=refreshed_count,
        skipped_existing=skipped_existing,
        skipped_external=skipped_external,
        failed_count=failed_count,
        invalid_count=len(invalid_refs),
    )
    return TaskResult(
        summary=summary,
        new_novel_titles=new_novel_titles,
        failed_ids=list(dict.fromkeys(failed_ids)),
    )


__all__ = ["SourceFetchArgs", "source_fetch"]
