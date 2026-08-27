"""Use case: batch download — package novels as ZIP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import BinaryIO

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import EpubStatus
from copixiv.core.services import build_batch_zip
from copixiv.core.services import NovelNamingTemplate
from copixiv.core.services import SearchConditions
from copixiv.core.services import QuerySpec
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


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
    titles: list[str]
    missing_ids: list[str]
    search_desc: str


class BatchDownloadUseCase:
    """Build a ZIP of matching novels.

    Raises:
        NotFoundError: If no matching novels are found or no valid files
            could be added to the ZIP.
    """

    def __init__(self, novel_repo: SQLAlchemyNovelRepository, naming_template: str | None = None):
        self._repo = novel_repo
        self._naming_template = naming_template

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
        excluded_ids: list[int] | None = None,
    ) -> BatchDownloadResult:
        if novel_ids:
            # Explicit selection — filters/order are irrelevant; a stable
            # id-descending order keeps the result deterministic.
            all_novels = await self._repo.get_novels_by_ids(novel_ids)
            novels = sorted(
                all_novels, key=lambda n: n.id or 0, reverse=True,
            )[:limit]
            if not novels:
                raise NotFoundError("未找到匹配条件的小说")
        else:
            results = await self._repo.get_novels(
                QuerySpec(
                    conditions=conditions or [],
                    order_by=order_by,
                    order_direction=order_direction,
                    per_page=limit,
                    min_like=min_like,
                    min_text=min_text,
                    exclude_ids=excluded_ids or [],
                )
            )
            novels = results.get("novels", [])
            if not novels:
                raise NotFoundError("未找到匹配条件的小说")

        naming = naming_template or self._naming_template
        try:
            # ZIP_DEFLATED compression is pure CPU — run it in a worker
            # thread so a large export (选多少下多少, no size cap) never
            # freezes the event loop / background tasks.
            zip_buf, titles, missing = await asyncio.to_thread(
                build_batch_zip, novels, format_mode, naming,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not titles:
            raise NotFoundError("未找到可下载的有效文件")

        zip_buf.seek(0)
        search_desc = zip_name or _build_search_desc(conditions or [])
        return BatchDownloadResult(
            zip_buffer=zip_buf,
            titles=titles,
            missing_ids=missing,
            search_desc=search_desc,
        )

    async def preview(
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
        excluded_ids: list[int] | None = None,
    ) -> str | None:
        """Resolve the naming template for the first matching novel.

        Returns the arcname (extension included) the first novel would get
        inside the ZIP — a live preview of the naming rule — or ``None``
        when no novels match.

        Raises:
            ValidationError: If the template does not contain ``{id}``.
        """
        if novel_ids:
            all_novels = await self._repo.get_novels_by_ids(novel_ids)
            novels = sorted(
                all_novels, key=lambda n: n.id or 0, reverse=True,
            )[:1]
        else:
            results = await self._repo.get_novels(
                QuerySpec(
                    conditions=conditions or [],
                    order_by=order_by,
                    order_direction=order_direction,
                    per_page=1,
                    min_like=min_like,
                    min_text=min_text,
                    exclude_ids=excluded_ids or [],
                )
            )
            novels = results.get("novels", [])
        if not novels:
            return None

        naming = (
            naming_template
            or self._naming_template
            or "{author_name}/{series_name}/#{series_index}_{title}_{id}"
        )
        try:
            template = NovelNamingTemplate(naming)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        novel = novels[0]
        actual_fmt = (
            "epub"
            if (format_mode == "prefer_epub"
                and novel.has_epub == EpubStatus.DONE)
            else "txt"
        )
        return template.resolve(novel) + "." + actual_fmt


def _build_search_desc(conditions: SearchConditions) -> str:
    """Build a human-readable description for the download filename."""
    search_desc = "批量下载"
    keywords = [value for qtype, value in conditions if qtype == "keyword"]
    if keywords:
        search_desc = "_".join(keywords[:3])
    return search_desc
