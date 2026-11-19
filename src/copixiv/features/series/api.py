"""Series API endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from copixiv.db.tx import read_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import PixivClientDep, UowDep
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.series.resolve import fetch_series_name


class SeriesLookupResponse(BaseModel):
    """系列 ID 预览（``GET /api/novels/series/{series_id}``）—— 与作者那份同构。

    差别只在名字的来源链：``series`` 行 → 同系列的 novel 行 → pixiv
    ``novel_series``（前两步都是本地查询，见 ``resolve_series.py``）。
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
):
    """系列 ID → 系列名预览（编辑器「系列」一栏的即时反馈）。

    与作者预览同构（见 ``lookup_author``）：只读、不写库，``unknown`` **不是
    错误**——id 才是身份，编辑器照样允许保存。名字的来源链更长一点：``series``
    行 → 同系列的 novel 行 → pixiv ``novel_series``（前两步都是本地查询）。
    """

    def _read_series(tx: SqlUnitOfWork) -> tuple[str | None, int]:
        repo = SQLAlchemySeriesRepository(tx.session)
        return repo.get_name_fallback(series_id), repo.count_novels_by_series(series_id)

    local_name, novel_count = await read_transaction(uow, _read_series)
    if local_name:
        return SeriesLookupResponse(
            series_id=series_id,
            series_name=local_name,
            source="local",
            novel_count=novel_count,
        )
    pixiv_name = await fetch_series_name(client, series_id)
    return SeriesLookupResponse(
        series_id=series_id,
        series_name=pixiv_name,
        source="pixiv" if pixiv_name else "unknown",
        novel_count=novel_count,
    )
