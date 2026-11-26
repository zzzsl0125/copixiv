"""Series API endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from copixiv.core.models import SOURCE_PIXIV
from copixiv.db.tx import read_transaction
from copixiv.deps import PixivClientDep, SourceServiceDep, UowDep
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.series.service import lookup_series_profile


class SeriesLookupResponse(BaseModel):
    """系列 ID 预览（``GET /api/novels/series/{series_id}``）—— 与作者那份同构。

    差别只在名字的来源链：``series`` 行 → 同系列的 novel 行 → pixiv
    ``novel_series``（前两步都是本地查询，见 ``features/series/service.py``）。
    """

    series_id: int
    series_name: str | None = None
    source: Literal["local", "pixiv", "unknown"] = "unknown"
    novel_count: int = 0


router = APIRouter()


@router.get("/{series_id}", response_model=SeriesLookupResponse)
async def lookup_series(
    series_id: int,
    uow: UowDep,
    client: PixivClientDep,
    source_service: SourceServiceDep,
):
    """系列 ID → 系列名预览（编辑器「系列」一栏的即时反馈）。

    与作者预览同构（见 ``lookup_author``）：只读、不写库，``unknown`` **不是
    错误**——id 才是身份，编辑器照样允许保存。名字的来源链更长一点：``series``
    行 → 同系列的 novel 行 → pixiv ``novel_series``（前两步都是本地查询）。
    """

    active_client = client if source_service.is_enabled(SOURCE_PIXIV) else None
    profile = await lookup_series_profile(series_id, uow=uow, client=active_client)
    novel_count = await read_transaction(
        uow,
        lambda tx: SQLAlchemySeriesRepository(tx.session).count_novels_by_series(series_id),
    )
    return SeriesLookupResponse(
        series_id=profile.series_id,
        series_name=profile.series_name or None,
        source=profile.source,
        novel_count=novel_count,
    )
