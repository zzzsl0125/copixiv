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


class TagAliasCreate(TagAliasBase):
    pass


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


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    task: str | None = None
    cron: str | None = None
    params: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    is_enabled: bool | None = None
    sort_index: int | None = None


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


# ---------------------------------------------------------------------------
# Batch Download
# ---------------------------------------------------------------------------

class BatchDownloadRequest(BaseModel):
    queries: str | None = None
    order_by: Literal["id", "like", "view", "text", "create_time", "random"] = "id"
    order_direction: Literal["ASC", "DESC"] = "DESC"
    min_like: int | None = None
    min_text: int | None = None
    limit: int = Field(default=50, ge=1, le=200)
    format_mode: Literal["txt", "prefer_epub"] = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


# ---------------------------------------------------------------------------
# Token Management
# ---------------------------------------------------------------------------

class TokenBase(BaseModel):
    name: str
    token: str
    premium: bool = False
    valid: bool = True


class TokenCreate(TokenBase):
    pass


class TokenUpdate(BaseModel):
    name: str | None = None
    token: str | None = None
    premium: bool | None = None
    valid: bool | None = None


class TokenResponse(TokenBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
