"""Use case: batch download — package novels as ZIP."""

from __future__ import annotations

import io
from dataclasses import dataclass

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.services.archive import build_batch_zip
from copixiv.infrastructure.repositories.novel import NovelRepository


@dataclass
class BatchDownloadRequest:
    queries: str | None = None
    order_by: str = "id"
    order_direction: str = "DESC"
    min_like: int | None = None
    min_text: int | None = None
    limit: int = 50
    format_mode: str = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


@dataclass
class BatchDownloadResult:
    """Result of a batch-download operation.

    Attributes:
        zip_buffer: In-memory ZIP file as a ``BytesIO``.
        titles: List of titles that were successfully added to the ZIP.
        missing_ids: List of novel IDs whose files were missing.
        search_desc: A human-readable description for the download filename.
    """

    zip_buffer: io.BytesIO
    titles: list[str]
    missing_ids: list[str]
    search_desc: str


class BatchDownloadUseCase:
    """Build a ZIP of matching novels.

    Raises:
        NotFoundError: If no matching novels are found or no valid files
            could be added to the ZIP.
    """

    def __init__(self, novel_repo: NovelRepository, naming_template: str | None = None):
        self._repo = novel_repo
        self._naming_template = naming_template

    async def execute(
        self, req: BatchDownloadRequest, queries: dict[str, str] | None = None
    ) -> BatchDownloadResult:
        results = await self._repo.get_novels(
            queries=queries,
            order_by=req.order_by,
            order_direction=req.order_direction,
            per_page=req.limit,
            min_like=req.min_like,
            min_text=req.min_text,
        )
        novels = results.get("novels", [])
        if not novels:
            raise NotFoundError("未找到匹配条件的小说")

        naming = req.naming_template or self._naming_template
        zip_buf, titles, missing = build_batch_zip(novels, req.format_mode, naming)
        if not titles:
            raise NotFoundError("未找到可下载的有效文件")

        zip_buf.seek(0)
        search_desc = req.zip_name or _build_search_desc(queries or {})
        return BatchDownloadResult(
            zip_buffer=zip_buf,
            titles=titles,
            missing_ids=missing,
            search_desc=search_desc,
        )


def _build_search_desc(queries: dict[str, str]) -> str:
    """Build a human-readable description for the download filename."""
    search_desc = f"批量下载"
    if queries:
        keywords = [k for k, v in queries.items() if v == "keyword"]
        if keywords:
            search_desc = f"{'_'.join(keywords[:3])}"
    return search_desc
