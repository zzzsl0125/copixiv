"""Web API Pydantic schemas — kept identical to v1 for frontend compatibility."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Domain enums — re-exported from domain.models to keep a single source
# of truth (values are part of the v1-compatible API contract).
# ---------------------------------------------------------------------------

from copixiv.domain.models.novel import EpubStatus
from copixiv.domain.models.tag import TagPreferenceType


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

def _parse_json_str(v: Any) -> Any:
    """Parse a JSON string to dict, or return the value as-is."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return v


# ---------------------------------------------------------------------------
# Tag Preferences
# ---------------------------------------------------------------------------

class TagPreferenceResponse(BaseModel):
    id: int
    tag: str
    preference: str  # "favourite" | "blocked"
    sort_index: int
    model_config = ConfigDict(from_attributes=True)


class TagPreferenceCreate(BaseModel):
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int = 0


class TagPreferenceUpdate(BaseModel):
    tag: str | None = None
    preference: Literal["favourite", "blocked"] | None = None
    sort_index: int | None = None


# ---------------------------------------------------------------------------
# Tag Aliases
# ---------------------------------------------------------------------------

class TagAliasBase(BaseModel):
    source: str
    target: str


class TagAliasResponse(TagAliasBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class TagCandidate(BaseModel):
    id: int
    name: str
    reference_count: int


class TagAliasSuggestResponse(BaseModel):
    target: TagCandidate
    candidates: list[TagCandidate]


class TagAliasSuggestListResponse(BaseModel):
    items: list[TagAliasSuggestResponse]
    next_offset: int


# ---------------------------------------------------------------------------
# Search History
# ---------------------------------------------------------------------------

class SearchHistoryResponse(BaseModel):
    id: int
    type: str
    value: str
    display_value: str | None = None
    timestamp: str
    model_config = ConfigDict(from_attributes=True)


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


# ---------------------------------------------------------------------------
# Task Management
# ---------------------------------------------------------------------------

class ScheduledTaskCreate(BaseModel):
    name: str
    task: str
    cron: str
    params: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    is_enabled: bool = False
    sort_index: int = 0

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        """Reject malformed cron expressions at the API boundary (422)
        instead of letting them fail silently inside the scheduler."""
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(v)
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
        return v


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    task: str | None = None
    cron: str | None = None
    params: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    is_enabled: bool | None = None
    sort_index: int | None = None

    @field_validator("cron")
    @classmethod
    def _validate_cron_optional(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return ScheduledTaskCreate._validate_cron(v)


class ScheduledTaskResponse(ScheduledTaskCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)

    _parse_config = field_validator("config", mode="before")(_parse_json_str)
    _parse_params = field_validator("params", mode="before")(_parse_json_str)


class TaskHistoryResponse(BaseModel):
    id: int
    name: str
    arguments: dict | None = None
    status: str
    start_time: str
    end_time: str | None = None
    duration: float | None = None
    result: dict | None = None
    model_config = ConfigDict(from_attributes=True)

    _parse_arguments = field_validator("arguments", mode="before")(_parse_json_str)
    _parse_result = field_validator("result", mode="before")(_parse_json_str)


class TaskHistoryListResponse(BaseModel):
    items: list[TaskHistoryResponse]
    total: int


class TaskArgument(BaseModel):
    name: str
    type: str
    default: Any | None = None
    required: bool


class TaskMethod(BaseModel):
    name: str
    description: str | None = None
    arguments: list[TaskArgument]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

class SystemConfigResponse(BaseModel):
    default_min_like: int
    default_min_text: int
    batch_download_naming: str
    exclude_blocked_tag_novels: bool = True


class SystemConfigUpdate(BaseModel):
    exclude_blocked_tag_novels: bool | None = None


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


# ---------------------------------------------------------------------------
# Token Management
# ---------------------------------------------------------------------------

class TokenBase(BaseModel):
    name: str
    token: str
    premium: bool = False
    valid: bool = True
    # Designated「追更账号」—— the account that owns the Pixiv following feed.
    is_follow: bool = False


class TokenUpdate(BaseModel):
    name: str | None = None
    token: str | None = None
    premium: bool | None = None
    valid: bool | None = None
    is_follow: bool | None = None


class TokenResponse(TokenBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Failed-novel ledger (下载失败)
# ---------------------------------------------------------------------------

class FailedNovelItem(BaseModel):
    """One download-failure record.

    ``title`` may be null for legacy rows recorded before title capture
    existed; ``last_failed_at`` may be null for pre-migration rows (they
    sort to the end of the list).
    """

    novel_id: int
    title: str | None = None
    failure_type: str | None = None
    error_message: str | None = None
    failed_times: int = 1
    last_failed_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class FailedNovelListResponse(BaseModel):
    items: list[FailedNovelItem]
    total: int
    offset: int = 0
    limit: int = 100


class FailedNovelCountResponse(BaseModel):
    count: int


class FailedNovelRetryRequest(BaseModel):
    novel_ids: list[int]
