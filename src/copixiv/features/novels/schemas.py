"""Novel / batch-operation Pydantic schemas — carried with the novels feature.

Moved out of the former ``web_api/schemas.py`` as part of the S1 structure
simplification.  The v2 API is now unfrozen — response details can evolve
freely (kept in sync with the client bundle).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from copixiv.core.models import EpubStatus


# ---------------------------------------------------------------------------
# Novels
# ---------------------------------------------------------------------------

class NovelBase(BaseModel):
    id: int
    title: str
    author_id: int | None = None
    author_name: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    caption: str | None = None
    create_time: str | None = None
    has_epub: EpubStatus = EpubStatus.NO
    tags: list[str] = []
    is_favourite: int = 0
    is_special_follow: int = 0
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    model_config = ConfigDict(from_attributes=True)

    # Coerce int columns from DB into proper types
    _coerce_has_epub = field_validator("has_epub", mode="before")(
        lambda v: EpubStatus(v) if v is not None else EpubStatus.NO
    )


class NovelListResponse(BaseModel):
    novels: list[NovelBase]
    cursor: dict | None = None
    # 首屏响应附带「当前搜索范围内是否存在被厌恶标签排除的小说」——
    # 前端据此决定 ExclusionBar（查看被隐藏的小说）是否显示；load-more
    # 与无关键词浏览不计算，保持默认 False。
    has_excluded: bool = False


# ---------------------------------------------------------------------------
# Batch Download
# ---------------------------------------------------------------------------

class BatchDownloadRequest(BaseModel):
    keyword: str | None = None
    order_by: Literal["id", "like", "view", "text", "create_time", "random"] = "id"
    order_direction: Literal["ASC", "DESC"] = "DESC"
    min_like: int | None = None
    min_text: int | None = None
    limit: int = Field(default=500, ge=1)
    format_mode: Literal["txt", "prefer_epub"] = "txt"
    zip_name: str | None = None
    naming_template: str | None = None
    # Batch-mode selection scope — when ``novel_ids`` is set, the download
    # is restricted to exactly those novels (filters are ignored).
    novel_ids: list[int] | None = None
    # IDs to skip — only applies to the filter-based path (novel_ids=None).
    excluded_ids: list[int] | None = None


# ---------------------------------------------------------------------------
# Batch operations (delete / add_tags / remove_tags)
# ---------------------------------------------------------------------------

class BatchScope(BaseModel):
    """Which novels a batch operation applies to.

    Two modes mirror the frontend selection model:
    - ``ids``: an explicit list of selected novel IDs.
    - ``all_matched``: the full filter-matched set (optionally minus
      ``excluded_ids`` — the "select all, then uncheck a few" case).
    """

    mode: Literal["ids", "all_matched"] = "all_matched"
    novel_ids: list[int] = []
    keyword: str | None = None
    min_like: int | None = None
    min_text: int | None = None
    excluded_ids: list[int] = []


class BatchOperationRequest(BaseModel):
    operation: Literal["delete", "add_tags", "remove_tags"]
    scope: BatchScope
    tags: list[str] = []


class BatchOperationResponse(BaseModel):
    matched: int
    affected: int


# ---------------------------------------------------------------------------
# Batch-mode selection helpers (search is a picking surface — the selection
# itself is an ID set independent of the current filters)
# ---------------------------------------------------------------------------

class NovelIdsResponse(BaseModel):
    """Matching novel IDs for the 「全选匹配」 bulk-add action.

    *limit*-truncated when the match set is larger than the batch cap;
    ``truncated`` tells the frontend to warn the user.
    """

    ids: list[int]
    total: int
    truncated: bool


class NovelsByIdsRequest(BaseModel):
    novel_ids: list[int]


class NovelsByIdsResponse(BaseModel):
    novels: list[NovelBase]
    truncated: bool


class MatchIdsRequest(BaseModel):
    """Intersect a selection with the current search scope (scoped clear)."""

    novel_ids: list[int]
    keyword: str | None = None
    min_like: int | None = None
    min_text: int | None = None


class SortIdsRequest(BaseModel):
    """Order an explicit id list by a novel column — 「查看已选」排序."""

    novel_ids: list[int]
    order_by: Literal["id", "like", "text"]
    order_direction: Literal["ASC", "DESC"] = "DESC"


class MatchIdsResponse(BaseModel):
    matching_ids: list[int]
    truncated: bool


class BatchTaskResponse(BaseModel):
    """A batch operation enqueued into the background task system."""

    task_id: int
    matched: int


class BatchExportRequest(BaseModel):
    """Background-task export (large selections — the ZIP is built offline)."""

    novel_ids: list[int]
    format_mode: Literal["txt", "prefer_epub"] = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


class BatchExportResponse(BaseModel):
    task_id: int
    matched: int
