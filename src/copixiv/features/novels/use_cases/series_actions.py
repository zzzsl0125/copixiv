"""Use cases for folded series-card actions: favourite, metadata edit, sync, delete."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import update

from copixiv.core.draft import NovelInfoLike
from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import SOURCE_PIXIV, UNKNOWN_AUTHOR
from copixiv.core.models import author_id as _author_id
from copixiv.core.sources import SourceCapability
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import ensure_parent_rows
from copixiv.db.tx import read_transaction, run_write_transaction, run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.service import lookup_author_profile
from copixiv.features.novels import field_policy
from copixiv.features.novels.ingest.book_chapter import BookChapterIngestor
from copixiv.features.novels.ingest.pipeline import ingest
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import NovelBase
from copixiv.features.novels.use_cases.batch_operations import delete_novel_paths
from copixiv.features.novels.use_cases.update_metadata import (
    MAX_TITLE,
    _check_length,
    _normalize_tags,
    _text,
)
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.tags.resolver import TagAliasResolver
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage


def toggle_series_favourite(series_id: int, *, uow: SqlUnitOfWork) -> bool:
    """Toggle ``is_favourite`` uniformly across all chapters of *series_id*."""
    return run_write_transaction_sync(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).toggle_series_favourite(series_id),
    )


async def update_series_metadata(
    series_id: int,
    changes: dict[str, Any],
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None = None,
) -> NovelBase:
    """Edit metadata for a folded series card (title/author where editable, plus union tags)."""
    if not changes:
        raise ValidationError("没有要更新的字段")

    def _prepare(tx: SqlUnitOfWork) -> tuple[str, list[str] | None, str | None]:
        novel_repo = SQLAlchemyNovelRepository(tx.session)
        series_meta, chapters = novel_repo.get_series_book_bundle(series_id)
        if not chapters:
            raise NotFoundError(f"Series {series_id} not found")
        source = (series_meta or {}).get("source") or str(chapters[0].source)

        for field in ("title", "author_id"):
            if field in changes and not field_policy.is_editable(source, field):
                raise ValidationError(field_policy.rejection_reason(field, source))

        clean_title: str | None = None
        if "title" in changes:
            clean_title = _text(changes["title"])
            if not clean_title:
                raise ValidationError("标题不能为空")
            _check_length("标题", clean_title, MAX_TITLE)

        norm_tags: list[str] | None = None
        if "tags" in changes:
            norm_tags = _normalize_tags(changes["tags"], TagAliasResolver(tx.session))

        return source, norm_tags, clean_title

    _source, norm_tags, clean_title = await read_transaction(uow, _prepare)

    author_values: dict[str, Any] = {}
    resolved_author: tuple[int, str] | None = None
    backfill_unknown_author = False
    target_author_id: int | None = None

    if "author_id" in changes:
        raw_author = changes["author_id"]
        if raw_author is None:
            author_values = {"author_id": None}
            backfill_unknown_author = True
        else:
            target_author_id = _author_id(raw_author)
            if target_author_id < 0:
                row = await read_transaction(
                    uow,
                    lambda tx: SQLAlchemyAuthorRepository(tx.session).get_by_id(target_author_id),
                )
                if row is None:
                    raise ValidationError("本地或外部作者不存在，请先从搜索结果中选择")
                author_values = {
                    "author_id": target_author_id,
                    "author_name": row.get("author_name"),
                }
            else:
                profile = await lookup_author_profile(target_author_id, uow=uow, client=client)
                author_values = {
                    "author_id": target_author_id,
                    "author_name": profile.author_name,
                }
                if profile.author_name:
                    resolved_author = (target_author_id, profile.author_name)

    def _commit(tx: SqlUnitOfWork) -> NovelBase:
        novel_repo = SQLAlchemyNovelRepository(tx.session)
        author_repo = SQLAlchemyAuthorRepository(tx.session)
        series_repo = SQLAlchemySeriesRepository(tx.session)

        _, cur_chapters = novel_repo.get_series_book_bundle(series_id)
        if not cur_chapters:
            raise NotFoundError(f"Series {series_id} not found")
        chapter_ids = [ch.id for ch in cur_chapters]

        if "author_id" in changes:
            if (
                target_author_id is not None
                and target_author_id < 0
                and author_repo.get_by_id(target_author_id) is None
            ):
                raise ValidationError("本地或外部作者不存在，请先从搜索结果中选择")
            if target_author_id is not None and target_author_id > 0:
                ensure_parent_rows(tx.session, author_ids={target_author_id})

            tx.session.execute(
                update(models.Series)
                .where(models.Series.series_id == series_id)
                .values(author_id=target_author_id if changes["author_id"] is not None else None)
            )
            novel_repo.update_metadata_many(chapter_ids, author_values)
            if backfill_unknown_author:
                novel_repo.update_metadata_many(
                    chapter_ids,
                    {"author_name": UNKNOWN_AUTHOR},
                    only_where_null="author_name",
                )
            if resolved_author is not None:
                author_repo.update_author_name(*resolved_author)

        if clean_title is not None:
            series_repo.update_series_name(series_id, clean_title)
            novel_repo.update_metadata_many(chapter_ids, {"series_name": clean_title})

        if norm_tags is not None:
            cur_union = {t for ch in cur_chapters for t in ch.tags}
            next_set = set(norm_tags)
            to_add = next_set - cur_union
            to_remove = cur_union - next_set
            if to_add:
                novel_repo.add_tags_to_novels(chapter_ids, to_add)
            if to_remove:
                novel_repo.remove_tags_from_novels(chapter_ids, to_remove)

        engine_delta.record_series_dirty(tx.session, [series_id])
        card = novel_repo.get_folded_series_card(series_id)
        if card is None:
            raise NotFoundError(f"Series {series_id} not found")
        return card

    return await run_write_transaction(uow, _commit)


async def sync_series(
    series_id: int,
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
    download_dir: str,
    source_registry: Any | None = None,
    source_service: Any | None = None,
    file_storage: Any | None = None,
    image_downloader: Any | None = None,
) -> NovelBase:
    """Re-fetch and incrementally sync all chapters of *series_id* from its upstream source."""

    def _read_bundle(tx: SqlUnitOfWork):
        return SQLAlchemyNovelRepository(tx.session).get_series_book_bundle(series_id)

    series_meta, chapters = await read_transaction(uow, _read_bundle)
    if series_meta is None and not chapters:
        raise NotFoundError(f"Series {series_id} not found")

    source = (series_meta or {}).get("source") or (str(chapters[0].source) if chapters else SOURCE_PIXIV)
    registry = (
        source_service.registry
        if source_service is not None and getattr(source_service, "registry", None) is not None
        else source_registry
    )
    can_sync = (
        registry.has_capability(source, SourceCapability.SYNC_NOVEL)
        if registry is not None
        else source == SOURCE_PIXIV
    )
    if not can_sync:
        raise ValidationError("本地或该数据源的系列不支持同步")
    if source_service is not None and not source_service.is_enabled(source):
        raise ValidationError(f"数据源 {source} 已禁用，无法同步")

    storage = file_storage or FileStorage(download_dir)

    if source != SOURCE_PIXIV:
        source_key = (series_meta or {}).get("source_key")
        if not source_key:
            raise ValidationError(f"系列 #{series_id} 缺少来源标识，无法同步")
        ingestor = BookChapterIngestor(
            session_factory=uow.session_factory,
            file_storage=storage,
            image_downloader=image_downloader,
            source_registry=registry,
            source_service=source_service,
        )
        await ingestor.ingest_book(source, str(source_key))
    else:
        if client is None:
            raise ValidationError("Pixiv 客户端未配置，无法同步")
        if series_id <= 0:
            raise ValidationError("本地或外部系列不支持 Pixiv 同步")
        resp = await client.novel_series(series_id, fetch_all=True)
        if not resp.novels:
            raise NotFoundError(f"系列 #{series_id} 在 pixiv 上不存在或没有章节")
        await ingest(
            cast(list[NovelInfoLike], resp.novels),
            session_factory=uow.session_factory,
            client=client,
            file_storage=storage,
            image_downloader=image_downloader,
        )
        series_title = getattr(resp.series, "title", None) or (
            getattr(resp.novels[0].series, "title", None)
            if resp.novels and getattr(resp.novels[0], "series", None)
            else None
        )
        if isinstance(series_title, str) and series_title.strip():
            await run_write_transaction(
                uow,
                lambda tx: SQLAlchemySeriesRepository(tx.session).update_series_name(
                    series_id, series_title.strip()
                ),
            )

    card = await read_transaction(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).get_folded_series_card(series_id),
    )
    if card is None:
        raise NotFoundError(f"Series {series_id} not found")
    return card


def delete_series(
    series_id: int,
    *,
    uow: SqlUnitOfWork,
    file_storage: FileStorage,
) -> int:
    """Delete all DB rows and local chapter files for *series_id*."""
    deleted_count, paths = run_write_transaction_sync(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).delete_series(series_id),
    )
    if deleted_count == 0:
        raise NotFoundError(f"Series {series_id} not found")
    delete_novel_paths(file_storage, paths)
    return deleted_count
