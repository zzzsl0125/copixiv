"""Novel query endpoints — search, counts, ID scoping, and sorting."""

from __future__ import annotations

import json
from typing import Annotated, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from copixiv.core.search import QuerySpec, parse_search_keyword
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import SessionFactoryDep, UowDep, require_api_key
from copixiv.features.novels.repo import NovelCount, SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import (
    BlockedIdsParams,
    MatchIdsRequest,
    MatchIdsResponse,
    NovelCountParams,
    NovelCountResponse,
    NovelFilterParams,
    NovelIdsResponse,
    NovelListParams,
    NovelListResponse,
    NovelsByIdsRequest,
    NovelsByIdsResponse,
    SortIdsRequest,
)
from copixiv.features.novels.use_cases.batch_operations import (
    BATCH_ID_CHUNK_SIZE,
    BATCH_MAX_NOVELS,
)
from copixiv.features.search_history.record import record_search_history

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
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        results = repo.get_novels(spec)

        if conditions and cursor_dict is None:
            results["has_excluded"] = repo.has_excluded_novels(spec)

    if conditions and background_tasks:
        background_tasks.add_task(
            record_search_history,
            conditions,
            lambda: SqlUnitOfWork(session_factory),
        )

    return results


@router.get("/count", response_model=NovelCountResponse)
def count_novels(
    uow: UowDep,
    params: Annotated[NovelCountParams, Query()],
):
    """匹配数（**有界**）+ 被厌恶标签隐藏数 + 两个截断标志。

    计数由 ``repo.COUNT_CAP`` 封顶（见 :class:`NovelCount`）：``total`` 保持
    老客户端的读法不变，但被截断时它只是下界 —— ``truncated: true`` 表示真实
    匹配数 > ``total``（调用方应显示 ``total+`` / ``≥ total``）。``excluded``
    同理，标志为 ``excluded_truncated``。两个标志恒在响应里，值只可能是
    布尔，不会缺席。
    """
    conditions = parse_search_keyword(params.keyword) if params.keyword else None
    spec = QuerySpec(
        conditions=conditions or [],
        min_like=params.min_like,
        min_text=params.min_text,
        exclude_ids=params.excluded_ids or [],
        exclude_blocked_tags=params.exclude_blocked,
    )
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        total = repo.count_novels(spec)
        excluded = NovelCount(0)
        if params.with_excluded and params.exclude_blocked is not False:
            excluded = repo.count_excluded_novels(spec)
    return NovelCountResponse(
        total=total.value,
        excluded=excluded.value,
        truncated=total.truncated,
        excluded_truncated=excluded.truncated,
    )


@router.get("/ids", response_model=NovelIdsResponse)
def get_matching_novel_ids(
    uow: UowDep,
    params: Annotated[NovelFilterParams, Query()],
):
    """All VISIBLE IDs matching the filters — powers the 「全选匹配」bulk-add action.

    No size cap: the selection itself may be any size (operations beyond
    the sync cap run as background tasks).  ``truncated`` is kept for wire
    compatibility and is always false here.

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
    with uow.begin():
        ids = SQLAlchemyNovelRepository(uow.session).list_matching_ids(spec)
    return NovelIdsResponse(ids=ids, total=len(ids), truncated=False)


@router.get("/blocked-ids", response_model=NovelIdsResponse)
def get_blocked_novel_ids(
    uow: UowDep,
    params: Annotated[BlockedIdsParams, Query()],
):
    """IDs of novels carrying blocked tags WITHIN the current search scope.

    Powers the 「查看被排除」view.  Follows the global exclusion setting:
    empty when exclusion is off.  The blocked-id list is filtered through
    the same scope logic as ``filter_ids_in_scope`` (chunked to keep each
    PostgreSQL query bounded); exclusion itself is not re-applied (the ids
    are already the excluded set).

    ``order_by`` (id/like/text) orders the result set; ``random`` or
    unsupported values return the scope order.
    """
    conditions = parse_search_keyword(params.keyword) if params.keyword else None
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        blocked_ids = repo.list_blocked_ids()
        if not blocked_ids:
            return NovelIdsResponse(ids=[], total=0, truncated=False)

        matching: list[int] = []
        for i in range(0, len(blocked_ids), BATCH_ID_CHUNK_SIZE):
            chunk = blocked_ids[i : i + BATCH_ID_CHUNK_SIZE]
            scope_spec = QuerySpec(
                conditions=conditions or [],
                min_like=params.min_like,
                min_text=params.min_text,
                exclude_blocked_tags=False,
            )
            matching.extend(repo.filter_ids_in_scope(chunk, scope_spec))

        if params.order_by and params.order_by != "random":
            matching = repo.sort_novel_ids(
                matching,
                params.order_by,
                params.order_direction,
            )
    return NovelIdsResponse(
        ids=matching,
        total=len(matching),
        truncated=False,
    )


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
        return NovelIdsResponse(ids=[], total=0, truncated=False)
    with uow.begin():
        ordered = SQLAlchemyNovelRepository(uow.session).sort_novel_ids(
            ids,
            body.order_by,
            body.order_direction,
        )
    return NovelIdsResponse(ids=ordered, total=len(ordered), truncated=False)


@router.post("/by-ids", response_model=NovelsByIdsResponse)
def get_novels_by_ids(
    body: NovelsByIdsRequest,
    uow: UowDep,
):
    """Novel details for an explicit ID list — powers the 「查看已选」view.

    Preserves the caller's ID order; missing IDs are dropped.  The input
    is capped at :data:`BATCH_MAX_NOVELS` (the view's page size — the
    frontend pages large selections in 5000-novel slices).
    """
    ids = body.novel_ids[:BATCH_MAX_NOVELS]
    truncated = len(body.novel_ids) > BATCH_MAX_NOVELS
    with uow.begin():
        novels = SQLAlchemyNovelRepository(uow.session).get_novel_cards_by_ids(ids)
    return NovelsByIdsResponse(
        novels=cast(list, novels),
        truncated=truncated,
    )


@router.post("/match-ids", response_model=MatchIdsResponse)
def match_novel_ids(
    body: MatchIdsRequest,
    uow: UowDep,
    exclude_blocked: Annotated[bool | None, Query()] = None,
):
    """Subset of *novel_ids* that match the filters — scoped 「清除选择」.

    Intersects the accumulated selection with the CURRENT search scope so
    the clear action only removes what belongs to the visible scope (the
    rest of the selection survives across searches).  Any-size input: the
    ID list is processed in internal chunks to keep each PostgreSQL query bounded.
    """
    conditions = parse_search_keyword(body.keyword) if body.keyword else None
    matching: list[int] = []
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        for i in range(0, len(body.novel_ids), BATCH_ID_CHUNK_SIZE):
            chunk = body.novel_ids[i : i + BATCH_ID_CHUNK_SIZE]
            scope_spec = QuerySpec(
                conditions=conditions or [],
                min_like=body.min_like,
                min_text=body.min_text,
                exclude_blocked_tags=exclude_blocked,
            )
            matching.extend(repo.filter_ids_in_scope(chunk, scope_spec))
    return MatchIdsResponse(matching_ids=matching, truncated=False)
