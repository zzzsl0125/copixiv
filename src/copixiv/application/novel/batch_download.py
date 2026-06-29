"""Use case: batch download — package novels as ZIP."""

import io
from dataclasses import dataclass

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


class BatchDownloadUseCase:
    """Build a ZIP of matching novels."""

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(
        self, req: BatchDownloadRequest, queries: dict[str, str] | None = None
    ) -> tuple[io.BytesIO, list[str], list[str]]:
        results = await self._repo.get_novels(
            queries=queries,
            order_by=req.order_by,
            order_direction=req.order_direction,
            per_page=req.limit,
            min_like=req.min_like,
            min_text=req.min_text,
        )
        novels = results.get("novels", [])
        zip_buf, titles, missing = build_batch_zip(novels, req.format_mode)
        zip_buf.seek(0)
        return zip_buf, titles, missing
