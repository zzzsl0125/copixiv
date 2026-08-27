"""Task management API schemas — carried with the task feature (S1)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _parse_json_str(v: Any) -> Any:
    """Parse a JSON string to dict, or return the value as-is."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return v


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
    progress: str | None = None
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
