"""Web API Pydantic schemas — kept identical to v1 for frontend compatibility."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from copixiv.infrastructure.database.models import TagPreferenceORM


class TagPreferenceResponse(BaseModel):
    id: int
    tag: str
    preference: TagPreferenceORM
    sort_index: int
    model_config = ConfigDict(from_attributes=True)


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


class SearchHistoryResponse(BaseModel):
    id: int
    type: str
    value: str
    display_value: str | None = None
    timestamp: str
    model_config = ConfigDict(from_attributes=True)


class NovelBase(BaseModel):
    id: int
    title: str
    author_name: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    caption: str | None = None
    create_time: str | None = None
    has_epub: int = 0
    tags: list[str] = []
    is_favourite: int = 0
    is_special_follow: int = 0
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    model_config = ConfigDict(from_attributes=True)


class NovelListResponse(BaseModel):
    novels: list[NovelBase]
    cursor: dict | None = None


# Task Management
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


class TaskHistoryListResponse(BaseModel):
    items: list[TaskHistoryResponse]
    total: int


class TaskArgument(BaseModel):
    name: str
    type: str
    default: Any | None = None
    required: bool


class SystemConfigResponse(BaseModel):
    default_min_like: int
    default_min_text: int


class TaskMethod(BaseModel):
    name: str
    description: str | None = None
    arguments: list[TaskArgument]


# Batch Download
class BatchDownloadRequest(BaseModel):
    queries: str | None = None
    order_by: str = "id"
    order_direction: str = "DESC"
    min_like: int | None = None
    min_text: int | None = None
    limit: int = 50
    format_mode: str = "txt"


# Token Management
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
