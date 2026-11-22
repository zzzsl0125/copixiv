"""Use case: batch download — package novels as ZIP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import BinaryIO

from copixiv.core import naming as formats
from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import EpubStatus
from copixiv.core.naming import DEFAULT_NAMING_TEMPLATE, NovelNamingTemplate
from copixiv.core.search import QuerySpec, SearchConditions
from copixiv.db.tx import read_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, StoredNovel
from copixiv.storage.zip import build_batch_zip


@dataclass
class BatchDownloadResult:
    """Result of a batch-download operation.

    Attributes:
        zip_buffer: The ZIP as a seekable binary file object (spooled:
            memory up to 8 MB, then disk).
        titles: List of titles that were successfully added to the ZIP.
        missing_ids: List of novel IDs whose files were missing.
        search_desc: A human-readable description for the download filename.
    """

    zip_buffer: BinaryIO
    added_count: int
    missing_ids: list[str]
    search_desc: str


class BatchDownloadUseCase:
    """Build a ZIP of matching novels.

    Raises:
        NotFoundError: If no matching novels are found or no valid files
            could be added to the ZIP.
    """

    def __init__(
        self,
        uow: SqlUnitOfWork,
        naming_template: str | None = None,
        *,
        download_dir: str | None = None,
    ):
        self._uow = uow
        self._naming_template = naming_template
        self._download_dir = download_dir

    async def execute(
        self,
        conditions: SearchConditions | None = None,
        *,
        order_by: str = "id",
        order_direction: str = "DESC",
        limit: int = 50,
        min_like: int | None = None,
        min_text: int | None = None,
        format_mode: str = "txt",
        zip_name: str | None = None,
        naming_template: str | None = None,
        novel_ids: list[int] | None = None,
    ) -> BatchDownloadResult:
        """Build the ZIP.

        DB reads run inside :func:`read_transaction` off the event loop, then
        ZIP compression runs via ``asyncio.to_thread``.
        """

        def _load_novels(tx: SqlUnitOfWork):
            repo = SQLAlchemyNovelRepository(tx.session)
            if novel_ids:
                # Explicit selection — filters/order are irrelevant; a stable
                # id-descending order keeps the result deterministic.
                all_novels = repo.get_novels_by_ids(novel_ids)
                selected = sorted(
                    all_novels,
                    key=lambda n: abs(n.id or 0),
                    reverse=True,
                )[:limit]
            else:
                results = repo.get_novels(
                    QuerySpec(
                        conditions=conditions or [],
                        order_by=order_by,
                        order_direction=order_direction,
                        per_page=limit,
                        min_like=min_like,
                        min_text=min_text,
                    )
                )
                selected_cards = results.get("novels", [])
                stored_paths = repo.get_stored_paths({c.id for c in selected_cards}) if selected_cards else {}
                selected = [
                    StoredNovel(
                        id=c.id,
                        title=c.title,
                        author_id=c.author_id,
                        author_name=c.author_name,
                        path=stored_paths.get(c.id),
                        like=c.like,
                        view=c.view,
                        text=c.text,
                        series_id=c.series_id,
                        series_name=c.series_name,
                        series_index=c.series_index,
                        create_time=c.create_time,
                        has_epub=c.has_epub,
                        source=c.source,
                        tags=c.tags,
                        is_favourite=c.is_favourite,
                        is_special_follow=c.is_special_follow,
                    )
                    for c in selected_cards
                ]
            return selected

        novels = await read_transaction(self._uow, _load_novels)
        if not novels:
            raise NotFoundError("未找到匹配条件的小说")

        naming = naming_template or self._naming_template
        try:
            # ZIP_DEFLATED compression is pure CPU — run it in a worker
            # thread so a large export (选多少下多少, no size cap) never
            # freezes the event loop / background tasks.
            zip_buf, added_count, missing = await asyncio.to_thread(
                build_batch_zip,
                novels,
                format_mode,
                naming,
                download_root=self._download_dir,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not added_count:
            raise NotFoundError("未找到可下载的有效文件")

        zip_buf.seek(0)
        search_desc = zip_name or _build_search_desc(conditions or [])
        return BatchDownloadResult(
            zip_buffer=zip_buf,
            added_count=added_count,
            missing_ids=missing,
            search_desc=search_desc,
        )

    def preview(
        self,
        conditions: SearchConditions | None = None,
        *,
        order_by: str = "id",
        order_direction: str = "DESC",
        min_like: int | None = None,
        min_text: int | None = None,
        format_mode: str = "txt",
        naming_template: str | None = None,
        novel_ids: list[int] | None = None,
    ) -> str | None:
        """Resolve the naming template for the first matching novel.

        Returns the arcname (extension included) the first novel would get
        inside the ZIP — a live preview of the naming rule — or ``None``
        when no novels match.

        Raises:
            ValidationError: If the template does not contain ``{id}``.
        """
        with self._uow.begin():
            repo = SQLAlchemyNovelRepository(self._uow.session)
            if novel_ids:
                novel = repo.get_latest_novel_by_ids(novel_ids)
            else:
                results = repo.get_novels(
                    QuerySpec(
                        conditions=conditions or [],
                        order_by=order_by,
                        order_direction=order_direction,
                        per_page=1,
                        min_like=min_like,
                        min_text=min_text,
                    )
                )
                novels = results.get("novels", [])
                novel = novels[0] if novels else None
        if novel is None:
            return None

        naming = naming_template or self._naming_template or DEFAULT_NAMING_TEMPLATE
        try:
            template = NovelNamingTemplate(naming)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        actual_fmt = formats.preferred_format(
            format_mode,
            epub_ready=novel.has_epub == EpubStatus.DONE,
        )
        return template.resolve(novel) + formats.suffix_for(actual_fmt)


def _build_search_desc(conditions: SearchConditions) -> str:
    """Build a human-readable description for the download filename."""
    search_desc = "批量下载"
    keywords = [value for qtype, value in conditions if qtype == "keyword"]
    if keywords:
        search_desc = "_".join(keywords[:3])
    return search_desc
