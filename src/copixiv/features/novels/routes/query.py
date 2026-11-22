"""Novel query endpoints — search, counts, ID scoping, and sorting."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from copixiv.core.search import QuerySpec, SearchConditions, parse_search_keyword
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import SessionFactoryDep, UowDep, require_api_key
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import (
    MatchIdsRequest,
    MatchIdsResponse,
    NovelCountResponse,
    NovelFilterParams,
    NovelIdsResponse,
    NovelListParams,
    NovelListResponse,
    NovelsByIdsRequest,
    NovelsByIdsResponse,
    SortIdsRequest,
)
from copixiv.features.search_history import record_search_history

_SHELF_NAVIGATION_TYPES = frozenset({"is_favourite", "is_special_follow", "source"})


def _filter_recordable_conditions(conditions: SearchConditions | None) -> SearchConditions:
    """Drop pure shelf-navigation DSL filters so only real search terms are recorded."""
    if not conditions:
        return []
    return [(qtype, value) for qtype, value in conditions if qtype not in _SHELF_NAVIGATION_TYPES]


router = APIRouter(dependencies=[Depends(require_api_key)])


def parse_json_param(value: str | None, name: str) -> dict | None:
    """Parse a JSON string query/cursor parameter into a dict."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {name} JSON format") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"Invalid {name} JSON format")
    return parsed


@router.get("", response_model=NovelListResponse)
def get_novels(
    background_tasks: BackgroundTasks,
    session_factory: SessionFactoryDep,
    uow: UowDep,
    params: Annotated[NovelListParams, Query()],
):
    conditions = parse_search_keyword(params.keyword) if params.keyword else None
    cursor_dict = parse_json_param(params.cursor, "cursor")

    spec = QuerySpec(
        conditions=conditions or [],
        order_by=params.order_by,
        order_direction=params.order_direction,
        cursor=cursor_dict,
        per_page=params.per_page,
        min_like=params.min_like,
        min_text=params.min_text,
        exclude_blocked_tags=params.exclude_blocked,
    )
    with uow.begin(read_only=True):
        repo = SQLAlchemyNovelRepository(uow.session)
        results = repo.get_novels(spec, only_blocked=params.only_blocked)

        if conditions and cursor_dict is None and not params.only_blocked:
            results["has_excluded"] = repo.has_excluded_novels(spec)

    recordable_conditions = _filter_recordable_conditions(conditions)
    if recordable_conditions and cursor_dict is None and not params.only_blocked and background_tasks:
        background_tasks.add_task(
            record_search_history,
            recordable_conditions,
            lambda: SqlUnitOfWork(session_factory),
        )

    return results


@router.get("/count", response_model=NovelCountResponse)
def count_novels(
    uow: UowDep,
    params: Annotated[NovelFilterParams, Query()],
):
    """匹配数（**有界**）+ 截断标志。

    计数由 ``repo.COUNT_CAP`` 封顶（见 :class:`NovelCount`）：``total`` 保持
    老客户端的读法不变，但被截断时它只是下界 —— ``truncated: true`` 表示真实
    匹配数 > ``total``（调用方应显示 ``total+`` / ``≥ total``）。
    """
    conditions = parse_search_keyword(params.keyword) if params.keyword else None
    spec = QuerySpec(
        conditions=conditions or [],
        min_like=params.min_like,
        min_text=params.min_text,
        exclude_blocked_tags=params.exclude_blocked,
    )
    with uow.begin(read_only=True):
        total = SQLAlchemyNovelRepository(uow.session).count_novels(spec)
    return NovelCountResponse(
        total=total.value,
        truncated=total.truncated,
    )


@router.get("/ids", response_model=NovelIdsResponse)
def get_matching_novel_ids(
    uow: UowDep,
    params: Annotated[NovelFilterParams, Query()],
):
    """All VISIBLE IDs matching the filters — powers the 「全选匹配」bulk-add action.

    No size cap: the selection itself may be any size (operations beyond
    the sync cap run as background tasks).

    ``total`` 直接取 ID 列表长度（精确、免费），**不**再打一次有界计数：
    有界计数的值会与这里完整返回的 ID 数不一致。
    """
    conditions = parse_search_keyword(params.keyword) if params.keyword else None
    spec = QuerySpec(
        conditions=conditions or [],
        min_like=params.min_like,
        min_text=params.min_text,
        exclude_blocked_tags=params.exclude_blocked,
    )
    with uow.begin(read_only=True):
        ids = SQLAlchemyNovelRepository(uow.session).list_matching_ids(spec)
    return NovelIdsResponse(ids=ids, total=len(ids))


@router.post("/sort-ids", response_model=NovelIdsResponse)
def sort_novel_ids(
    body: SortIdsRequest,
    uow: UowDep,
):
    """Order an explicit id list by a novel column — 「查看已选」排序.

    Returns the same ids ordered by id/like/view/text/create_time (missing ids dropped).
    """
    ids = sorted({int(i) for i in body.novel_ids})
    if not ids:
        return NovelIdsResponse(ids=[], total=0)
    with uow.begin(read_only=True):
        ordered = SQLAlchemyNovelRepository(uow.session).sort_novel_ids(
            ids,
            body.order_by,
            body.order_direction,
        )
    return NovelIdsResponse(ids=ordered, total=len(ordered))


@router.post("/by-ids", response_model=NovelsByIdsResponse)
def get_novels_by_ids(
    body: NovelsByIdsRequest,
    uow: UowDep,
):
    """Novel details for an explicit ID list — powers the 「查看已选」view.

    Preserves the caller's ID order; missing IDs are dropped.
    """
    with uow.begin(read_only=True):
        novels = SQLAlchemyNovelRepository(uow.session).get_novel_cards_by_ids(body.novel_ids)
    return NovelsByIdsResponse(novels=novels)


@router.post("/match-ids", response_model=MatchIdsResponse)
def match_novel_ids(
    body: MatchIdsRequest,
    uow: UowDep,
    exclude_blocked: Annotated[bool | None, Query()] = None,
):
    """Subset of *novel_ids* that match the filters — scoped 「清除选择」.

    Intersects the accumulated selection with the CURRENT search scope so
    the clear action only removes what belongs to the visible scope (the
    rest of the selection survives across searches).
    """
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    scope_spec = QuerySpec(
        conditions=conditions or [],
        min_like=body.min_like,
        min_text=body.min_text,
        exclude_blocked_tags=exclude_blocked,
    )
    with uow.begin(read_only=True):
        matching = SQLAlchemyNovelRepository(uow.session).filter_ids_in_scope(
            body.novel_ids,
            scope_spec,
        )
    return MatchIdsResponse(matching_ids=matching)
