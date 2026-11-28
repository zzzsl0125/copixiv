"""Multi-chapter external source ingestor (books, chapters, single-chapter sync, and failure retry)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from copixiv.core.draft import NovelDraft, build_novel
from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import EpubStatus
from copixiv.core.sources import (
    ExternalBookOutline,
    ExternalChapter,
    ExternalChapterEntry,
    SourceCapability,
    SourceCredentialRecord,
    SourcePlugin,
    SourceRuntimeConfig,
)
from copixiv.core.text import body_unchanged, has_image_placeholders, parse_tags
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.ingest.pipeline import IngestOutcome
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, StoredNovel
from copixiv.features.sources.entity_resolver import EntityResolver, lock_local_ids
from copixiv.features.tags.resolver import TagAliasResolver
from copixiv.log import logger
from copixiv.sources.esj.client import EsjChallengeError
from copixiv.sources.registry import SourceRegistry

if TYPE_CHECKING:
    from copixiv.features.sources.service import SourceService


@dataclass(frozen=True)
class BookIngestOutcome(IngestOutcome):
    """Ingest outcome extended with multi-chapter book and source-key failure metrics."""

    skipped_external: int = 0
    skipped_existing: int = 0
    failed_keys: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failure_count(self) -> int:
        """Total number of failed chapters/items across ID and key ledgers."""
        return max(len(self.failed_keys), len(self.failed_ids))


@dataclass(frozen=True)
class _ExistingChapterRow:
    id: int
    source_key: str
    path: str | None
    title: str


@dataclass(frozen=True)
class _FetchedChapterItem:
    source_key: str
    chapter_key: str
    series_index: int | None
    chapter: ExternalChapter
    fallback_title: str = ""


@dataclass(frozen=True)
class _RetryTarget:
    row_id: int | None
    source_key: str
    novel_id: int | None
    book_id: str | None
    chapter_id: str | None
    title: str | None
    extra: dict[str, Any] | None


@dataclass
class _PersistBatchResult:
    titles: list[str] = field(default_factory=list)
    new_titles: list[str] = field(default_factory=list)
    new_author_ids: set[int] = field(default_factory=set)
    new_count: int = 0
    rewritten_ids: set[int] = field(default_factory=set)
    failed_with_ids: list[tuple[int, str]] = field(default_factory=list)
    failed_keys: list[tuple[str, str]] = field(default_factory=list)


def _split_chapter_key(source_key: str, fallback_book_key: str = "") -> tuple[str, str]:
    cleaned = source_key.strip()
    if "/" in cleaned:
        book_part, chapter_part = cleaned.split("/", 1)
        return book_part.strip() or fallback_book_key, chapter_part.strip()
    return fallback_book_key, cleaned


def _format_create_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return dt.isoformat()


def _normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _resolve_author_attribution(
    outline: ExternalBookOutline,
    chapter: ExternalChapter | None = None,
) -> tuple[str, str]:
    """Resolve ``(author_source_key, author_name)`` from outline + optional chapter poster."""
    extra_name = outline.extra.get("author_name")
    detail_name = (
        extra_name.strip()
        if isinstance(extra_name, str) and extra_name.strip()
        else (outline.author.name.strip() if outline.author and outline.author.name else "")
    )

    if "is_original" in outline.extra:
        is_original = bool(outline.extra.get("is_original"))
        poster_uid: str | None = None
        poster_name = ""
        if chapter is not None and chapter.author is not None:
            if chapter.author.source_key.startswith("uid:"):
                poster_uid = chapter.author.source_key.removeprefix("uid:").strip() or None
            if chapter.author.name:
                poster_name = chapter.author.name.strip()

        if is_original:
            resolved_name = (
                (detail_name if detail_name and detail_name != "佚名" else poster_name)
                or detail_name
                or "佚名"
            )
            if poster_uid:
                return f"uid:{poster_uid}", resolved_name
            return f"name:{resolved_name}", resolved_name

        resolved_name = detail_name or "佚名"
        return f"name:{resolved_name}", resolved_name

    if outline.author is None and chapter is not None and chapter.author is not None:
        return chapter.author.source_key, (chapter.author.name or "佚名")
    if outline.author is not None:
        name = detail_name or "佚名"
        key = outline.author.source_key or f"name:{name}"
        return key, name
    return "name:佚名", "佚名"


def _resolve_book_author(
    outline: ExternalBookOutline,
    fetched_items: Sequence[_FetchedChapterItem],
) -> tuple[str, str]:
    """Resolve book/series-level ``(author_source_key, author_name)``."""
    if bool(outline.extra.get("is_original")):
        for item in fetched_items:
            key, name = _resolve_author_attribution(outline, item.chapter)
            if key.startswith("uid:"):
                return key, name
        if outline.author is not None and outline.author.source_key.startswith("uid:"):
            return outline.author.source_key, (outline.author.name or "佚名")
    return _resolve_author_attribution(outline, None)


def _merge_sticky_book_author(
    current: tuple[str, str] | None,
    outline: ExternalBookOutline,
    batch_items: Sequence[_FetchedChapterItem],
) -> tuple[str, str]:
    """Preserve a resolved ``uid:`` book author across incremental chapter flushes."""
    candidate = _resolve_book_author(outline, batch_items)
    if current is None:
        return candidate
    if not current[0].startswith("uid:") and candidate[0].startswith("uid:"):
        return candidate
    return current


DEFAULT_BOOK_BATCH_SIZE = 25


def _extract_retry_target(item: Any) -> _RetryTarget:
    """Normalise a ``FailedNovel`` object, tuple ``(id, key, nid, extra)``, or mapping."""
    row_id: int | None = None
    source_key = ""
    novel_id: int | None = None
    title: str | None = None
    extra_raw: Any = None

    if isinstance(item, (tuple, list)):
        if len(item) >= 4:
            row_id, source_key, novel_id, extra_raw = item[0], str(item[1] or ""), item[2], item[3]
        elif len(item) == 3:
            row_id, source_key, novel_id = item[0], str(item[1] or ""), item[2]
        elif len(item) == 2:
            row_id, source_key = item[0], str(item[1] or "")
    elif isinstance(item, Mapping):
        row_id = item.get("id")
        source_key = str(item.get("source_key") or "")
        novel_id = item.get("novel_id")
        title = item.get("title")
        extra_raw = item.get("extra")
    else:
        row_id = getattr(item, "id", None)
        source_key = str(getattr(item, "source_key", "") or "")
        novel_id = getattr(item, "novel_id", None)
        title = getattr(item, "title", None)
        extra_raw = getattr(item, "extra", None)

    extra = dict(extra_raw) if isinstance(extra_raw, Mapping) else None
    book_id_raw = extra.get("book_id") if extra else None
    chapter_id_raw = extra.get("chapter_id") if extra else None
    book_id = str(book_id_raw).strip() if book_id_raw is not None and str(book_id_raw).strip() else None
    chapter_id = (
        str(chapter_id_raw).strip() if chapter_id_raw is not None and str(chapter_id_raw).strip() else None
    )

    cleaned_key = source_key.strip()
    if "/" in cleaned_key:
        split_book, split_chapter = cleaned_key.split("/", 1)
        book_id = book_id or (split_book.strip() or None)
        chapter_id = chapter_id or (split_chapter.strip() or None)
    elif cleaned_key and book_id is None and chapter_id is None:
        book_id = cleaned_key

    return _RetryTarget(
        row_id=int(row_id) if row_id is not None else None,
        source_key=cleaned_key,
        novel_id=int(novel_id) if novel_id is not None else None,
        book_id=book_id,
        chapter_id=chapter_id,
        title=str(title) if title else None,
        extra=extra,
    )


async def _invoke_fetch_chapter(
    plugin: SourcePlugin,
    book_key: str,
    chapter_key: str,
    runtime: SourceRuntimeConfig,
    creds: Sequence[SourceCredentialRecord],
    *,
    password: str | None = None,
) -> ExternalChapter:
    """Call ``plugin.fetch_chapter``, passing ``password`` only when provided or supported."""
    if password is not None:
        params = inspect.signature(plugin.fetch_chapter).parameters
        if "password" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return await plugin.fetch_chapter(book_key, chapter_key, runtime, creds, password=password)
    return await plugin.fetch_chapter(book_key, chapter_key, runtime, creds)


def _unlink_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


class BookChapterIngestor:
    """Ingest, sync, and retry pipeline for multi-chapter book/chapter external sources."""

    DEFAULT_BATCH_SIZE = DEFAULT_BOOK_BATCH_SIZE

    def __init__(
        self,
        *,
        session_factory: Any,
        file_storage: Any,
        image_downloader: Any,
        source_registry: SourceRegistry | None = None,
        source_service: SourceService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._file_storage = file_storage
        self._image_downloader = image_downloader
        self._source_service = source_service
        self._source_registry: SourceRegistry | None = (
            source_registry if source_registry is not None else getattr(source_service, "registry", None)
        )

    def _require_plugin(
        self,
        source: str,
        *,
        capability: SourceCapability | None = None,
    ) -> SourcePlugin:
        if self._source_service is not None:
            plugin: SourcePlugin | None = self._source_service.require_enabled(source)
        else:
            if self._source_registry is None:
                raise ValidationError(f"未配置数据源注册表，无法处理 {source}")
            plugin = self._source_registry.get(source)
            if plugin is None:
                raise ValidationError(f"未知数据源: {source}")
        assert plugin is not None

        if capability is not None and not plugin.manifest.has_capability(capability):
            raise ValidationError(f"数据源 {plugin.manifest.display_name} 不支持该操作")
        return plugin

    async def _load_runtime_and_creds(
        self,
        source: str,
        uow: SqlUnitOfWork,
    ) -> tuple[SourceRuntimeConfig, Sequence[SourceCredentialRecord]]:
        if self._source_service is not None:

            def _read(uw: SqlUnitOfWork) -> tuple[SourceRuntimeConfig, Sequence[SourceCredentialRecord]]:
                assert self._source_service is not None
                return (
                    self._source_service.get_runtime_config(source, session=uw.session),
                    self._source_service.get_credentials(source, session=uw.session),
                )

            return await read_transaction(uow, _read)

        plugin = self._source_registry.get(source) if self._source_registry is not None else None
        defaults = plugin.manifest.default_config() if plugin is not None else {}
        return SourceRuntimeConfig.from_values(source, True, defaults), ()

    async def _record_chapter_failure(
        self,
        uow: SqlUnitOfWork,
        *,
        source: str,
        source_key: str,
        novel_id: int | None,
        title: str | None,
        reason: str,
        book_id: str,
        chapter_id: str,
        series_index: int | None,
    ) -> None:
        extra: dict[str, Any] = {"book_id": book_id, "chapter_id": chapter_id}
        if series_index is not None:
            extra["series_index"] = series_index

        def _write(tx: SqlUnitOfWork) -> None:
            FailedNovelRepository(tx.session).record(
                novel_id,
                "download",
                reason,
                title=title,
                source=source,
                source_key=source_key,
                extra=extra,
            )

        await run_write_transaction(uow, _write)

    async def ingest_book(
        self,
        source: str,
        book_key: str,
        *,
        force: bool = False,
        batch_size: int = DEFAULT_BOOK_BATCH_SIZE,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> BookIngestOutcome:
        """Fetch an external book outline and all non-external chapters needing ingest."""
        plugin = self._require_plugin(source, capability=SourceCapability.FETCH_BY_REF)
        uow = SqlUnitOfWork(self._session_factory)
        runtime, creds = await self._load_runtime_and_creds(source, uow)

        outline = await plugin.fetch_book_outline(book_key, runtime, creds)

        skipped_external = 0
        seen_keys: set[str] = set()
        non_external_entries: list[ExternalChapterEntry] = []
        for entry in outline.chapter_entries:
            if entry.is_external_link:
                skipped_external += 1
                continue
            if entry.source_key in seen_keys:
                continue
            seen_keys.add(entry.source_key)
            non_external_entries.append(entry)

        if not non_external_entries:
            return BookIngestOutcome(skipped_external=skipped_external)

        target_keys = {e.source_key for e in non_external_entries}

        def _plan_read(
            uw: SqlUnitOfWork,
        ) -> tuple[dict[str, _ExistingChapterRow], set[str]]:
            rows = uw.session.execute(
                select(
                    models.Novel.id,
                    models.Novel.source_key,
                    models.Novel.path,
                    models.Novel.title,
                ).where(
                    models.Novel.source == source,
                    models.Novel.source_key.in_(target_keys),
                )
            ).all()
            existing_map = {
                skey: _ExistingChapterRow(id=int(nid), source_key=skey, path=path, title=title)
                for nid, skey, path, title in rows
            }
            skip_keys = (
                set() if force else FailedNovelRepository(uw.session).get_skip_keys(source, target_keys)
            )
            return existing_map, skip_keys

        existing_by_key, skip_keys = await read_transaction(uow, _plan_read)

        to_fetch: list[ExternalChapterEntry] = []
        metadata_updates: list[tuple[ExternalChapterEntry, _ExistingChapterRow]] = []
        for entry in non_external_entries:
            existing = existing_by_key.get(entry.source_key)
            if force or (existing is None and entry.source_key not in skip_keys):
                to_fetch.append(entry)
            elif existing is not None:
                metadata_updates.append((entry, existing))

        effective_batch_size = max(1, int(batch_size))
        book_label = (outline.series.title or "").strip() or book_key
        total_to_fetch = len(to_fetch)
        logger.info(
            f"开始抓取 {source} 书本《{book_label}》({book_key}): "
            f"共 {len(non_external_entries)} 章，待抓取 {total_to_fetch} 章，"
            f"跳过已有 {len(metadata_updates)} 章，跳过外链 {skipped_external} 条"
        )

        fetched_buffer: list[_FetchedChapterItem] = []
        all_titles: list[str] = []
        all_new_titles: list[str] = []
        all_author_ids: set[int] = set()
        all_failed_with_ids: list[tuple[int, str]] = []
        all_failed_keys: list[tuple[str, str]] = []
        all_rewritten_ids: set[int] = set()
        total_new_count = 0
        persisted_count = 0
        metadata_applied = False
        sticky_book_author: tuple[str, str] | None = None
        aborted_challenge: EsjChallengeError | None = None
        last_progress_at = time.monotonic()

        if on_progress is not None and total_to_fetch > 0:
            await on_progress(f"正在抓取《{book_label}》：0/{total_to_fetch} 章")
            last_progress_at = time.monotonic()

        async def _flush_batch(batch_items: list[_FetchedChapterItem]) -> None:
            nonlocal metadata_applied, persisted_count, sticky_book_author, total_new_count
            pending_meta = metadata_updates if not metadata_applied else []
            if not batch_items and not pending_meta:
                return
            sticky_book_author = _merge_sticky_book_author(sticky_book_author, outline, batch_items)
            batch_res = await self._persist_and_process_chapters(
                uow,
                source=source,
                book_key=book_key,
                outline=outline,
                fetched_items=batch_items,
                metadata_updates=pending_meta,
                force=force,
                use_body_diff=False,
                book_author_override=sticky_book_author,
            )
            metadata_applied = True
            all_titles.extend(batch_res.titles)
            all_new_titles.extend(batch_res.new_titles)
            all_author_ids.update(batch_res.new_author_ids)
            all_failed_with_ids.extend(batch_res.failed_with_ids)
            all_failed_keys.extend(batch_res.failed_keys)
            all_rewritten_ids.update(batch_res.rewritten_ids)
            total_new_count += batch_res.new_count
            persisted_count += len(batch_items)
            if batch_items:
                logger.info(
                    f"抓取 {source} 书本《{book_label}》({book_key}) 分批落库: "
                    f"本批 {len(batch_items)} 章（累计 {persisted_count}/{total_to_fetch} 章，新增 {total_new_count} 本）"
                )

        for idx, entry in enumerate(to_fetch, start=1):
            ch_book_key, ch_id = _split_chapter_key(entry.source_key, book_key)
            try:
                chapter = await _invoke_fetch_chapter(
                    plugin,
                    ch_book_key,
                    ch_id,
                    runtime,
                    creds,
                )
            except asyncio.CancelledError:
                raise
            except EsjChallengeError as exc:
                logger.warning(f"抓取 {source} 书本 {book_key} 时触发站点验证，中止剩余章节: {exc}")
                aborted_challenge = exc
                break
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                logger.warning(f"抓取 {source} 章节 {entry.source_key} 失败: {reason}")
                existing = existing_by_key.get(entry.source_key)
                known_id = existing.id if existing is not None else None
                await self._record_chapter_failure(
                    uow,
                    source=source,
                    source_key=entry.source_key,
                    novel_id=known_id,
                    title=entry.title,
                    reason=reason,
                    book_id=book_key,
                    chapter_id=ch_id,
                    series_index=entry.series_index,
                )
                all_failed_keys.append((entry.source_key, reason))
                if known_id is not None:
                    all_failed_with_ids.append((known_id, reason))
            else:
                fetched_buffer.append(
                    _FetchedChapterItem(
                        source_key=entry.source_key,
                        chapter_key=ch_id,
                        series_index=entry.series_index,
                        chapter=chapter,
                        fallback_title=entry.title,
                    )
                )

            flushed = False
            if len(fetched_buffer) >= effective_batch_size:
                batch = list(fetched_buffer)
                fetched_buffer.clear()
                await _flush_batch(batch)
                flushed = True

            if on_progress is not None and (flushed or time.monotonic() - last_progress_at >= 3.0):
                await on_progress(
                    f"正在抓取《{book_label}》：{idx}/{total_to_fetch} 章（已入库 {persisted_count} 章）"
                )
                last_progress_at = time.monotonic()

        if fetched_buffer or not metadata_applied:
            remaining = list(fetched_buffer)
            fetched_buffer.clear()
            await _flush_batch(remaining)
            if on_progress is not None and total_to_fetch > 0 and remaining and aborted_challenge is None:
                await on_progress(
                    f"正在抓取《{book_label}》：{total_to_fetch}/{total_to_fetch} 章（已入库 {persisted_count} 章）"
                )

        if aborted_challenge is not None:
            raise aborted_challenge

        return BookIngestOutcome(
            titles=all_titles,
            new_titles=all_new_titles,
            new_author_ids=all_author_ids,
            failed=all_failed_with_ids,
            new_count=total_new_count,
            rewritten_ids=all_rewritten_ids,
            skipped_external=skipped_external,
            skipped_existing=len(metadata_updates),
            failed_keys=all_failed_keys,
        )

    async def ingest_chapter(
        self,
        source: str,
        book_key: str,
        chapter_key: str,
        *,
        force: bool = False,
        password: str | None = None,
    ) -> BookIngestOutcome:
        """Fetch a single chapter alongside its book outline, persist, and process assets."""
        plugin = self._require_plugin(source, capability=SourceCapability.FETCH_BY_REF)
        uow = SqlUnitOfWork(self._session_factory)
        runtime, creds = await self._load_runtime_and_creds(source, uow)

        clean_book = book_key.strip()
        clean_chapter = chapter_key.split("/", 1)[1].strip() if "/" in chapter_key else chapter_key.strip()
        target_source_key = f"{clean_book}/{clean_chapter}"

        existing = await read_transaction(
            uow,
            lambda uw: self._read_existing_one(uw, source, target_source_key),
        )
        known_id = existing.id if existing is not None else None

        try:
            outline = await plugin.fetch_book_outline(clean_book, runtime, creds)
        except asyncio.CancelledError:
            raise
        except EsjChallengeError:
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            logger.warning(f"抓取 {source} 书本目录 {clean_book} 失败: {reason}")
            await self._record_chapter_failure(
                uow,
                source=source,
                source_key=target_source_key,
                novel_id=known_id,
                title=existing.title if existing else None,
                reason=reason,
                book_id=clean_book,
                chapter_id=clean_chapter,
                series_index=None,
            )
            return BookIngestOutcome(
                failed=[(known_id, reason)] if known_id is not None else [],
                failed_keys=[(target_source_key, reason)],
            )

        matched_entry = next(
            (
                e
                for e in outline.chapter_entries
                if not e.is_external_link and e.source_key == target_source_key
            ),
            None,
        )
        series_index = matched_entry.series_index if matched_entry is not None else None
        fallback_title = (
            matched_entry.title if matched_entry is not None else (existing.title if existing else "")
        )

        try:
            chapter = await _invoke_fetch_chapter(
                plugin,
                clean_book,
                clean_chapter,
                runtime,
                creds,
                password=password,
            )
        except asyncio.CancelledError:
            raise
        except EsjChallengeError:
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            logger.warning(f"抓取 {source} 章节 {target_source_key} 失败: {reason}")
            await self._record_chapter_failure(
                uow,
                source=source,
                source_key=target_source_key,
                novel_id=known_id,
                title=fallback_title or None,
                reason=reason,
                book_id=clean_book,
                chapter_id=clean_chapter,
                series_index=series_index,
            )
            return BookIngestOutcome(
                failed=[(known_id, reason)] if known_id is not None else [],
                failed_keys=[(target_source_key, reason)],
            )

        effective_index = chapter.series_index if chapter.series_index is not None else series_index
        batch_res = await self._persist_and_process_chapters(
            uow,
            source=source,
            book_key=clean_book,
            outline=outline,
            fetched_items=[
                _FetchedChapterItem(
                    source_key=target_source_key,
                    chapter_key=clean_chapter,
                    series_index=effective_index,
                    chapter=chapter,
                    fallback_title=fallback_title,
                )
            ],
            metadata_updates=[],
            force=force,
            use_body_diff=True,
        )

        return BookIngestOutcome(
            titles=batch_res.titles,
            new_titles=batch_res.new_titles,
            new_author_ids=batch_res.new_author_ids,
            failed=batch_res.failed_with_ids,
            new_count=batch_res.new_count,
            rewritten_ids=batch_res.rewritten_ids,
            failed_keys=batch_res.failed_keys,
        )

    async def sync_chapter(self, novel_id: int, *, uow: SqlUnitOfWork) -> StoredNovel:
        """Re-fetch authoritative chapter/book metadata and non-empty body for single-novel sync."""

        def _read_target(
            uw: SqlUnitOfWork,
        ) -> (
            tuple[str, str | None, str | None, int | None, str | None, int | None, str | None, list[str]]
            | None
        ):
            row = uw.session.get(models.Novel, novel_id)
            if row is None:
                return None
            series_key: str | None = None
            sibling_paths: list[str] = []
            if row.series_id is not None:
                series_key = uw.session.execute(
                    select(models.Series.source_key).where(models.Series.series_id == row.series_id)
                ).scalar_one_or_none()
                sibling_paths = [
                    p
                    for p in uw.session.execute(
                        select(models.Novel.path).where(
                            models.Novel.series_id == row.series_id,
                            models.Novel.path.isnot(None),
                        )
                    )
                    .scalars()
                    .all()
                    if p
                ]
            return (
                str(row.source),
                row.source_key,
                row.path,
                row.series_id,
                row.series_name,
                row.series_index,
                series_key,
                sibling_paths,
            )

        target = await read_transaction(uow, _read_target)
        if target is None:
            raise NotFoundError(f"Novel {novel_id} not found")

        (
            novel_source,
            source_key,
            stored_path,
            existing_series_id,
            existing_series_name,
            existing_series_index,
            existing_series_key,
            sibling_paths,
        ) = target
        plugin = self._require_plugin(novel_source, capability=SourceCapability.SYNC_NOVEL)
        if not source_key:
            raise ValidationError(f"小说 #{novel_id} 缺少来源标识 source_key，无法同步")

        runtime, creds = await self._load_runtime_and_creds(novel_source, uow)

        chapter = await plugin.fetch_novel_for_sync(source_key, stored_path, runtime, creds)
        if chapter is None:
            raise NotFoundError(f"小说 #{novel_id} 在数据源 {novel_source} 上不存在或不可访问")

        anchor: str | None = None
        saved_path: Path | None = None
        if chapter.content:
            if self._file_storage is None:
                raise RuntimeError("FileStorage is required for sync_chapter")
            anchor = self._file_storage.adopt_anchor(stored_path)
            series_dir = (
                self._file_storage.find_existing_series_dir(sibling_paths, novel_source)
                if anchor is None and existing_series_id is not None
                else None
            )
            saved_path = await asyncio.to_thread(
                self._file_storage.save_novel_text,
                novel_id,
                chapter.title or source_key,
                chapter.content,
                anchor=anchor,
                source=novel_source,
                series_id=existing_series_id,
                series_name=existing_series_name,
                series_key=existing_series_key,
                series_index=(
                    chapter.series_index if chapter.series_index is not None else existing_series_index
                ),
                series_dir=series_dir,
            )

        def _persist_sync(tx: SqlUnitOfWork) -> None:
            session = tx.session
            novel_row = session.get(models.Novel, novel_id)
            if novel_row is None:
                raise NotFoundError(f"Novel {novel_id} not found")

            resolver = EntityResolver(session)
            author_id = novel_row.author_id
            author_name = novel_row.author_name
            if chapter.author is not None:
                author_id = resolver.resolve_author(
                    novel_source,
                    chapter.author.source_key,
                    name=chapter.author.name,
                )
                author_name = chapter.author.name

            series_id = novel_row.series_id
            series_name = novel_row.series_name
            if novel_row.series_id is not None:
                series_row = session.get(models.Series, novel_row.series_id)
                if series_row is not None:
                    series_id = resolver.resolve_series(
                        novel_source,
                        series_row.source_key,
                        name=series_row.series_name,
                        author_id=author_id,
                    )
                    series_name = series_row.series_name
            elif "/" in source_key:
                book_key = source_key.split("/", 1)[0].strip()
                if book_key:
                    series_id = resolver.resolve_series(
                        novel_source,
                        book_key,
                        name=novel_row.series_name,
                        author_id=author_id,
                    )
                    series_name = novel_row.series_name

            if chapter.title:
                novel_row.title = chapter.title
            if author_id is not None:
                novel_row.author_id = author_id
            if author_name is not None:
                novel_row.author_name = author_name
            if series_id is not None:
                novel_row.series_id = series_id
            if series_name is not None:
                novel_row.series_name = series_name
            if chapter.series_index is not None:
                novel_row.series_index = chapter.series_index
            novel_row.like = int(chapter.like)
            novel_row.view = int(chapter.view)
            if chapter.create_time is not None:
                novel_row.create_time = _normalize_utc_datetime(chapter.create_time)
            if chapter.content:
                novel_row.text = len(chapter.content)
                if saved_path is not None and (not novel_row.path or anchor is None):
                    novel_row.path = str(saved_path)

            if chapter.tags:
                resolved_tags = TagAliasResolver(session).resolve_set(parse_tags(chapter.tags))
                current_tags = set(novel_row.tags or [])
                if resolved_tags - current_tags:
                    novel_row.tags = sorted(current_tags | resolved_tags)

            session.flush()
            engine_delta.record_novel_upserts(session, [novel_id])

        await run_write_transaction(uow, _persist_sync)

        updated = await read_transaction(
            uow,
            lambda uw: SQLAlchemyNovelRepository(uw.session).get_by_id(novel_id),
        )
        if updated is None:
            raise NotFoundError(f"Novel {novel_id} not found")
        return updated

    async def retry_failures(
        self,
        source: str,
        items: Sequence[Any],
        *,
        batch_size: int = DEFAULT_BOOK_BATCH_SIZE,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> BookIngestOutcome:
        """Retry failed chapters/books grouped by ``book_id`` to share outline requests."""
        if not items:
            return BookIngestOutcome()

        plugin = self._require_plugin(source, capability=SourceCapability.RETRY_FAILURE)
        uow = SqlUnitOfWork(self._session_factory)
        runtime, creds = await self._load_runtime_and_creds(source, uow)
        effective_batch_size = max(1, int(batch_size))

        targets = [_extract_retry_target(item) for item in items]
        book_level_targets: list[_RetryTarget] = []
        grouped_chapters: dict[str, list[_RetryTarget]] = {}
        invalid_targets: list[_RetryTarget] = []

        for target in targets:
            if target.book_id and target.chapter_id:
                grouped_chapters.setdefault(target.book_id, []).append(target)
            elif target.book_id:
                book_level_targets.append(target)
            else:
                invalid_targets.append(target)

        all_titles: list[str] = []
        all_new_titles: list[str] = []
        all_author_ids: set[int] = set()
        all_failed_ids: list[tuple[int, str]] = []
        all_failed_keys: list[tuple[str, str]] = []
        all_rewritten_ids: set[int] = set()
        total_new_count = 0

        for target in invalid_targets:
            reason = f"无法解析重试目标 source_key={target.source_key!r}"
            all_failed_keys.append((target.source_key, reason))
            if target.novel_id is not None:
                all_failed_ids.append((target.novel_id, reason))

        for target in book_level_targets:
            assert target.book_id is not None
            try:
                out = await self.ingest_book(
                    source,
                    target.book_id,
                    force=True,
                    batch_size=effective_batch_size,
                    on_progress=on_progress,
                )
            except EsjChallengeError:
                raise
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                all_failed_keys.append((target.source_key, reason))
                if target.novel_id is not None:
                    all_failed_ids.append((target.novel_id, reason))
                continue
            all_titles.extend(out.titles)
            all_new_titles.extend(out.new_titles)
            all_author_ids.update(out.new_author_ids)
            all_failed_ids.extend(out.failed)
            all_failed_keys.extend(out.failed_keys)
            all_rewritten_ids.update(out.rewritten_ids)
            total_new_count += out.new_count

        aborted_challenge: EsjChallengeError | None = None
        total_retry_chapters = sum(len(g) for g in grouped_chapters.values())
        processed_chapters = 0
        persisted_chapters = 0
        last_progress_at = time.monotonic()

        if on_progress is not None and total_retry_chapters > 0:
            await on_progress(f"正在重试 {source} 失败章节：0/{total_retry_chapters}")
            last_progress_at = time.monotonic()

        for book_id, group in grouped_chapters.items():
            try:
                outline = await plugin.fetch_book_outline(book_id, runtime, creds)
            except asyncio.CancelledError:
                raise
            except EsjChallengeError as exc:
                aborted_challenge = exc
                break
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                logger.warning(f"重试 {source} 书本目录 {book_id} 失败: {reason}")
                for target in group:
                    assert target.chapter_id is not None
                    s_key = target.source_key or f"{book_id}/{target.chapter_id}"
                    raw_idx = target.extra.get("series_index") if target.extra else None
                    series_index = int(raw_idx) if isinstance(raw_idx, int) else None
                    await self._record_chapter_failure(
                        uow,
                        source=source,
                        source_key=s_key,
                        novel_id=target.novel_id,
                        title=target.title,
                        reason=reason,
                        book_id=book_id,
                        chapter_id=target.chapter_id,
                        series_index=series_index,
                    )
                    all_failed_keys.append((s_key, reason))
                    if target.novel_id is not None:
                        all_failed_ids.append((target.novel_id, reason))
                processed_chapters += len(group)
                if (
                    on_progress is not None
                    and total_retry_chapters > 0
                    and time.monotonic() - last_progress_at >= 3.0
                ):
                    await on_progress(
                        f"正在重试 {source} 失败章节：{processed_chapters}/{total_retry_chapters}（已恢复 {persisted_chapters} 章）"
                    )
                    last_progress_at = time.monotonic()
                continue

            entry_by_key = {e.source_key: e for e in outline.chapter_entries if not e.is_external_link}
            fetched_buffer: list[_FetchedChapterItem] = []
            sticky_book_author: tuple[str, str] | None = None

            async def _flush_retry_batch(
                batch_items: list[_FetchedChapterItem],
                *,
                target_book_id: str = book_id,
                target_outline: ExternalBookOutline = outline,
            ) -> None:
                nonlocal persisted_chapters, sticky_book_author, total_new_count
                if not batch_items:
                    return
                sticky_book_author = _merge_sticky_book_author(
                    sticky_book_author, target_outline, batch_items
                )
                batch_res = await self._persist_and_process_chapters(
                    uow,
                    source=source,
                    book_key=target_book_id,
                    outline=target_outline,
                    fetched_items=batch_items,
                    metadata_updates=[],
                    force=True,
                    use_body_diff=False,
                    book_author_override=sticky_book_author,
                )
                all_titles.extend(batch_res.titles)
                all_new_titles.extend(batch_res.new_titles)
                all_author_ids.update(batch_res.new_author_ids)
                all_failed_ids.extend(batch_res.failed_with_ids)
                all_failed_keys.extend(batch_res.failed_keys)
                all_rewritten_ids.update(batch_res.rewritten_ids)
                total_new_count += batch_res.new_count
                persisted_chapters += len(batch_items)

            for target in group:
                assert target.chapter_id is not None
                s_key = target.source_key or f"{book_id}/{target.chapter_id}"
                entry = entry_by_key.get(s_key)
                raw_idx = target.extra.get("series_index") if target.extra else None
                fallback_index = int(raw_idx) if isinstance(raw_idx, int) else None
                series_index = entry.series_index if entry is not None else fallback_index
                fallback_title = entry.title if entry is not None else (target.title or "")

                try:
                    chapter = await _invoke_fetch_chapter(
                        plugin,
                        book_id,
                        target.chapter_id,
                        runtime,
                        creds,
                    )
                except asyncio.CancelledError:
                    raise
                except EsjChallengeError as exc:
                    aborted_challenge = exc
                    break
                except Exception as exc:
                    reason = str(exc) or type(exc).__name__
                    logger.warning(f"重试 {source} 章节 {s_key} 失败: {reason}")
                    await self._record_chapter_failure(
                        uow,
                        source=source,
                        source_key=s_key,
                        novel_id=target.novel_id,
                        title=fallback_title or None,
                        reason=reason,
                        book_id=book_id,
                        chapter_id=target.chapter_id,
                        series_index=series_index,
                    )
                    all_failed_keys.append((s_key, reason))
                    if target.novel_id is not None:
                        all_failed_ids.append((target.novel_id, reason))
                else:
                    effective_index = (
                        chapter.series_index if chapter.series_index is not None else series_index
                    )
                    fetched_buffer.append(
                        _FetchedChapterItem(
                            source_key=s_key,
                            chapter_key=target.chapter_id,
                            series_index=effective_index,
                            chapter=chapter,
                            fallback_title=fallback_title,
                        )
                    )

                processed_chapters += 1
                flushed = False
                if len(fetched_buffer) >= effective_batch_size:
                    batch = list(fetched_buffer)
                    fetched_buffer.clear()
                    await _flush_retry_batch(batch)
                    flushed = True

                if on_progress is not None and (flushed or time.monotonic() - last_progress_at >= 3.0):
                    await on_progress(
                        f"正在重试 {source} 失败章节：{processed_chapters}/{total_retry_chapters}（已恢复 {persisted_chapters} 章）"
                    )
                    last_progress_at = time.monotonic()

            if fetched_buffer:
                remaining = list(fetched_buffer)
                fetched_buffer.clear()
                await _flush_retry_batch(remaining)
                if (
                    on_progress is not None
                    and total_retry_chapters > 0
                    and remaining
                    and aborted_challenge is None
                ):
                    await on_progress(
                        f"正在重试 {source} 失败章节：{processed_chapters}/{total_retry_chapters}（已恢复 {persisted_chapters} 章）"
                    )
                    last_progress_at = time.monotonic()

            if aborted_challenge is not None:
                break

        if aborted_challenge is not None:
            raise aborted_challenge

        return BookIngestOutcome(
            titles=all_titles,
            new_titles=all_new_titles,
            new_author_ids=all_author_ids,
            failed=all_failed_ids,
            new_count=total_new_count,
            rewritten_ids=all_rewritten_ids,
            failed_keys=all_failed_keys,
        )

    @staticmethod
    def _read_existing_one(
        uw: SqlUnitOfWork,
        source: str,
        source_key: str,
    ) -> _ExistingChapterRow | None:
        row = uw.session.execute(
            select(models.Novel.id, models.Novel.source_key, models.Novel.path, models.Novel.title).where(
                models.Novel.source == source,
                models.Novel.source_key == source_key,
            )
        ).one_or_none()
        if row is None:
            return None
        return _ExistingChapterRow(id=int(row[0]), source_key=row[1], path=row[2], title=row[3])

    async def _persist_and_process_chapters(
        self,
        uow: SqlUnitOfWork,
        *,
        source: str,
        book_key: str,
        outline: ExternalBookOutline,
        fetched_items: list[_FetchedChapterItem],
        metadata_updates: list[tuple[ExternalChapterEntry, _ExistingChapterRow]],
        force: bool,
        use_body_diff: bool,
        book_author_override: tuple[str, str] | None = None,
    ) -> _PersistBatchResult:
        """Persist fetched chapters + metadata updates in a short write tx, then run assets."""
        if not fetched_items and not metadata_updates:
            return _PersistBatchResult()

        local_texts: dict[str, str | None] = {}
        if use_body_diff and not force and self._file_storage is not None:
            existing_rows = await read_transaction(
                uow,
                lambda uw: self._load_existing_rows_by_key(
                    uw,
                    source,
                    {item.source_key for item in fetched_items},
                ),
            )
            for item in fetched_items:
                existing = existing_rows.get(item.source_key)
                if existing is not None and existing.path:
                    anchor = self._file_storage.adopt_anchor(existing.path)
                    if anchor:
                        local_texts[item.source_key] = await asyncio.to_thread(
                            self._file_storage.read_novel_text,
                            existing.id,
                            existing.title,
                            anchor=anchor,
                            source=source,
                        )

        drafts, resolved_author_ids, new_titles, new_count = await run_write_transaction(
            uow,
            lambda tx: self._write_chapters_tx(
                tx,
                source=source,
                book_key=book_key,
                outline=outline,
                fetched_items=fetched_items,
                metadata_updates=metadata_updates,
                force=force,
                use_body_diff=use_body_diff,
                local_texts=local_texts,
                book_author_override=book_author_override,
            ),
        )

        result = _PersistBatchResult(
            titles=[d.title for d in drafts if d.title],
            new_titles=new_titles,
            new_author_ids=resolved_author_ids,
            new_count=new_count,
            rewritten_ids={d.id for d in drafts if d.rewrote_body},
        )

        asset_drafts = [d for d in drafts if d.images or d.cover_url or has_image_placeholders(d.content)]
        if self._image_downloader is None or not asset_drafts:
            return result

        batch = self._image_downloader.new_batch()
        for draft in asset_drafts:
            await self._image_downloader.process_novel_assets(
                draft,
                force=(force or draft.rewrote_body),
                batch=batch,
            )

        asset_failures = await batch.await_all()
        outcomes = batch.drain_outcomes()

        done_ids = [nid for nid, out in outcomes.items() if out == "done"]
        no_images_ids = [nid for nid, out in outcomes.items() if out == "no_images"]
        draft_by_id = {d.id: d for d in drafts}
        for nid, out in outcomes.items():
            if out == "skipped":
                draft_obj = draft_by_id.get(nid)
                if (
                    draft_obj is not None
                    and draft_obj.has_epub == EpubStatus.PENDING
                    and not draft_obj.images
                    and not has_image_placeholders(draft_obj.content)
                ):
                    no_images_ids.append(nid)

        failed_asset_map: dict[int, str] = dict(asset_failures)
        for nid, out in outcomes.items():
            if out == "failed" and nid not in failed_asset_map:
                failed_asset_map[nid] = f"资产处理失败: novel {nid}"

        if done_ids or no_images_ids or failed_asset_map:

            def _apply_asset_statuses(tx: SqlUnitOfWork) -> None:
                novel_repo = SQLAlchemyNovelRepository(tx.session)
                if done_ids:
                    novel_repo.update_has_epub_status(done_ids, EpubStatus.DONE)
                if no_images_ids:
                    novel_repo.update_has_epub_status(no_images_ids, EpubStatus.NO_IMAGES)
                if failed_asset_map:
                    novel_repo.update_has_epub_status(list(failed_asset_map), EpubStatus.PENDING)
                    failed_repo = FailedNovelRepository(tx.session)
                    for nid, reason in failed_asset_map.items():
                        d = draft_by_id.get(nid)
                        skey = d.source_key if d and d.source_key else str(nid)
                        b_id, c_id = _split_chapter_key(skey, book_key)
                        failed_repo.record(
                            nid,
                            "asset",
                            reason,
                            title=d.title if d else None,
                            source=source,
                            source_key=skey,
                            extra={
                                "book_id": b_id,
                                "chapter_id": c_id,
                                "series_index": d.series_index if d else None,
                            },
                        )

            await run_write_transaction(uow, _apply_asset_statuses)

        for nid, reason in failed_asset_map.items():
            d = draft_by_id.get(nid)
            result.failed_with_ids.append((nid, reason))
            if d and d.source_key:
                result.failed_keys.append((d.source_key, reason))

        return result

    @staticmethod
    def _load_existing_rows_by_key(
        uw: SqlUnitOfWork,
        source: str,
        source_keys: set[str],
    ) -> dict[str, _ExistingChapterRow]:
        if not source_keys:
            return {}
        rows = uw.session.execute(
            select(models.Novel.id, models.Novel.source_key, models.Novel.path, models.Novel.title).where(
                models.Novel.source == source,
                models.Novel.source_key.in_(source_keys),
            )
        ).all()
        return {
            skey: _ExistingChapterRow(id=int(nid), source_key=skey, path=path, title=title)
            for nid, skey, path, title in rows
        }

    def _write_chapters_tx(
        self,
        tx: SqlUnitOfWork,
        *,
        source: str,
        book_key: str,
        outline: ExternalBookOutline,
        fetched_items: list[_FetchedChapterItem],
        metadata_updates: list[tuple[ExternalChapterEntry, _ExistingChapterRow]],
        force: bool,
        use_body_diff: bool,
        local_texts: dict[str, str | None],
        book_author_override: tuple[str, str] | None = None,
    ) -> tuple[list[NovelDraft], set[int], list[str], int]:
        """Short synchronous write callback: resolve entities, write files, upsert, refresh metadata."""
        session = tx.session
        resolver = EntityResolver(session)
        novel_repo = SQLAlchemyNovelRepository(session)
        failed_repo = FailedNovelRepository(session)
        tag_resolver = TagAliasResolver(session)

        # 1. Determine unique authors and resolve in deterministic key order.
        book_author_key, book_author_name = (
            book_author_override
            if book_author_override is not None
            else _resolve_book_author(outline, fetched_items)
        )
        author_pairs: dict[str, str] = {book_author_key: book_author_name}
        item_author_keys: list[str] = []
        for item in fetched_items:
            akey, aname = _resolve_author_attribution(outline, item.chapter)
            item_author_keys.append(akey)
            if akey not in author_pairs or (aname and aname != "佚名"):
                author_pairs[akey] = aname

        author_ids_by_key = {
            akey: resolver.resolve_author(source, akey, name=author_pairs[akey])
            for akey in sorted(author_pairs)
        }
        book_author_id = author_ids_by_key[book_author_key]

        # 2. Resolve series row.
        series_key = outline.series.source_key or book_key
        series_name = outline.series.title
        series_id = resolver.resolve_series(
            source,
            series_key,
            name=series_name,
            author_id=book_author_id,
        )

        # 3. Re-query existing chapters inside write tx (serialize new ID allocation if needed).
        fetched_keys = {item.source_key for item in fetched_items}
        existing_by_key: dict[str, tuple[int, str | None]] = {}
        if fetched_keys:
            for nid, skey, spath in session.execute(
                select(models.Novel.id, models.Novel.source_key, models.Novel.path).where(
                    models.Novel.source == source,
                    models.Novel.source_key.in_(fetched_keys),
                )
            ).all():
                existing_by_key[skey] = (int(nid), spath)

            missing_keys = fetched_keys - set(existing_by_key)
            if missing_keys:
                lock_local_ids(session)
                for nid, skey, spath in session.execute(
                    select(models.Novel.id, models.Novel.source_key, models.Novel.path).where(
                        models.Novel.source == source,
                        models.Novel.source_key.in_(missing_keys),
                    )
                ).all():
                    existing_by_key[skey] = (int(nid), spath)

        # 4. Write novel text files and assemble drafts.
        if fetched_items and self._file_storage is None:
            raise RuntimeError("FileStorage is required when persisting fetched chapters")

        series_dir: Path | None = None
        if fetched_items and self._file_storage is not None:
            series_sibling_paths = [
                p
                for p in session.execute(
                    select(models.Novel.path).where(
                        models.Novel.series_id == series_id,
                        models.Novel.path.isnot(None),
                    )
                )
                .scalars()
                .all()
                if p
            ]
            series_dir = self._file_storage.find_existing_series_dir(series_sibling_paths, source)

        created_files: list[Path] = []
        drafts: list[NovelDraft] = []
        force_groups: dict[tuple[str, ...], list[NovelDraft]] = {}
        existing_ids_before: set[int] = set()
        resolved_author_ids: set[int] = set()

        try:
            for item, akey in zip(fetched_items, item_author_keys, strict=True):
                ch_author_id = author_ids_by_key[akey]
                ch_author_name = author_pairs[akey]
                resolved_author_ids.add(ch_author_id)

                existing_info = existing_by_key.get(item.source_key)
                if existing_info is not None:
                    novel_id, stored_path = existing_info
                    existing_ids_before.add(novel_id)
                else:
                    novel_id = resolver.next_novel_id(lock=False)
                    stored_path = None

                title = item.chapter.title.strip() or item.fallback_title or f"第{item.chapter_key}章"
                anchor = self._file_storage.adopt_anchor(stored_path) if stored_path else None

                if existing_info is not None and use_body_diff and not force:
                    local_body = local_texts.get(item.source_key)
                    unchanged = body_unchanged(local_body, item.chapter.content)
                    rewrote = bool(item.chapter.content) and not unchanged
                else:
                    rewrote = True

                if rewrote:
                    target_path = (
                        Path(anchor)
                        if anchor
                        else self._file_storage.novel_text_path(
                            novel_id,
                            title,
                            source=source,
                            series_id=series_id,
                            series_name=series_name,
                            series_key=series_key,
                            series_index=item.series_index,
                            series_dir=series_dir,
                        )
                    )
                    existed_before = target_path.exists()
                    saved_path = self._file_storage.save_novel_text(
                        novel_id,
                        title,
                        item.chapter.content,
                        anchor=anchor,
                        source=source,
                        series_id=series_id,
                        series_name=series_name,
                        series_key=series_key,
                        series_index=item.series_index,
                        series_dir=series_dir,
                    )
                    if not existed_before:
                        created_files.append(saved_path)
                    if anchor is None and series_dir is None:
                        series_dir = saved_path.parent
                else:
                    saved_path = Path(
                        anchor
                        or self._file_storage.novel_text_path(
                            novel_id,
                            title,
                            source=source,
                            series_id=series_id,
                            series_name=series_name,
                            series_key=series_key,
                            series_index=item.series_index,
                            series_dir=series_dir,
                        )
                    )

                has_images = bool(item.chapter.images) or has_image_placeholders(item.chapter.content)
                if existing_info is not None and not rewrote and anchor is not None:
                    has_epub: EpubStatus | None = None
                else:
                    has_epub = EpubStatus.PENDING if has_images else EpubStatus.NO_IMAGES

                combined_tags: list[str | dict[Any, Any]] = list(
                    dict.fromkeys([*(outline.tags or ()), *(item.chapter.tags or ())])
                )
                like_val = item.chapter.like if item.chapter.like else outline.like
                view_val = item.chapter.view if item.chapter.view else outline.view
                create_time_str = _format_create_time(item.chapter.create_time)

                draft = build_novel(
                    id=novel_id,
                    title=title,
                    author_id=ch_author_id,
                    author_name=ch_author_name,
                    path=str(saved_path),
                    like=like_val,
                    view=view_val,
                    text=len(item.chapter.content),
                    caption=None,
                    series_id=series_id,
                    series_name=series_name,
                    series_index=item.series_index,
                    create_time=create_time_str,
                    has_epub=has_epub,
                    tags=combined_tags,
                    content=item.chapter.content,
                    images=dict(item.chapter.images) if item.chapter.images else None,
                    cover_url=item.chapter.cover_url or outline.series.cover_url,
                    source=source,
                    source_key=item.source_key,
                    download_dir=self._file_storage.download_dir,
                )
                draft = replace(draft, rewrote_body=rewrote)
                drafts.append(draft)

                force_fields = ["author_id", "author_name", "series_id"]
                if rewrote or anchor is None:
                    force_fields.append("path")
                if draft.series_index is not None:
                    force_fields.append("series_index")
                if draft.create_time is not None:
                    force_fields.append("create_time")
                force_groups.setdefault(tuple(sorted(force_fields)), []).append(draft)

            new_count = 0
            for force_tuple, group_drafts in force_groups.items():
                new_count += novel_repo.upsert_novels(group_drafts, list(force_tuple))

            succeeded_keys = {d.source_key for d in drafts if d.source_key}
            if succeeded_keys:
                failed_repo.forget_by_source_keys(source, succeeded_keys)
        except BaseException:
            _unlink_paths(created_files)
            raise

        new_titles = [d.title for d in drafts if d.title and d.id not in existing_ids_before]

        # 5. Targeted metadata update for existing chapters NOT re-downloaded (force=False).
        if metadata_updates:
            self._update_existing_metadata_rows(
                session=session,
                tag_resolver=tag_resolver,
                outline=outline,
                series_id=series_id,
                series_name=series_name,
                metadata_updates=metadata_updates,
            )

        return drafts, resolved_author_ids, new_titles, new_count

    @staticmethod
    def _update_existing_metadata_rows(
        *,
        session: Any,
        tag_resolver: TagAliasResolver,
        outline: ExternalBookOutline,
        series_id: int,
        series_name: str | None,
        metadata_updates: list[tuple[ExternalChapterEntry, _ExistingChapterRow]],
    ) -> None:
        ids_to_update = [row.id for _, row in metadata_updates]
        orm_rows = {
            int(obj.id): obj
            for obj in session.execute(select(models.Novel).where(models.Novel.id.in_(ids_to_update)))
            .scalars()
            .all()
        }
        incoming_tags = tag_resolver.resolve_set(parse_tags(outline.tags or ()))
        changed_ids: list[int] = []

        for entry, old_row in metadata_updates:
            orm_row = orm_rows.get(old_row.id)
            if orm_row is None:
                continue
            row_changed = False
            if orm_row.series_index != entry.series_index:
                orm_row.series_index = entry.series_index
                row_changed = True
            if orm_row.series_id != series_id:
                orm_row.series_id = series_id
                row_changed = True
            if series_name is not None and orm_row.series_name != series_name:
                orm_row.series_name = series_name
                row_changed = True
            if orm_row.like != int(outline.like):
                orm_row.like = int(outline.like)
                row_changed = True
            if orm_row.view != int(outline.view):
                orm_row.view = int(outline.view)
                row_changed = True
            if incoming_tags:
                current_tags = set(orm_row.tags or [])
                if incoming_tags - current_tags:
                    orm_row.tags = sorted(current_tags | incoming_tags)
                    row_changed = True
            if row_changed:
                changed_ids.append(int(orm_row.id))

        if changed_ids:
            session.flush()
            engine_delta.record_novel_upserts(session, changed_ids)


__all__ = ["BookChapterIngestor", "BookIngestOutcome"]
