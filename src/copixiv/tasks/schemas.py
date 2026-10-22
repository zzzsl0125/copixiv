"""Task management API schemas — carried with the task feature (S1)."""

from __future__ import annotations

import json
from datetime import datetime
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
    is_enabled: bool = False
    sort_index: int = 0

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        """Reject malformed cron expressions at the API boundary (422).

        Keeps the previous 5-field crontab contract: exactly five
        whitespace-separated fields (minute hour day month weekday).
        Validity itself is delegated to :func:`copixiv.tasks.kernel.next_occurrence`
        (croniter), whose ``ValueError`` becomes a Pydantic 422.
        """
        from datetime import timezone

        from copixiv.tasks.kernel import next_occurrence

        if len(v.split()) != 5:
            raise ValueError(
                "Invalid cron expression: exactly 5 fields are required "
                "(minute hour day-of-month month day-of-week)"
            )
        try:
            next_occurrence(v, datetime.now(timezone.utc))
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
        return v


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    task: str | None = None
    cron: str | None = None
    params: dict[str, Any] | None = None
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

    _parse_params = field_validator("params", mode="before")(_parse_json_str)


class TaskHistoryResponse(BaseModel):
    id: int
    name: str
    # Registered function name (``task_history.task_func``); ``name`` is the
    # user-facing display label.  NULL on legacy rows written before the
    # column existed.
    task_func: str | None = None
    arguments: dict | None = None
    status: str
    # timestamptz → datetime; Pydantic serializes to an ISO string on the wire.
    start_time: datetime
    end_time: datetime | None = None
    duration: float | None = None
    result: dict | None = None
    # ``progress`` is a JSONB column.  Tasks write a plain message string; keep
    # it permissive (Any) so a future dict-shaped progress doesn't break the
    # wire contract.
    progress: Any | None = None
    model_config = ConfigDict(from_attributes=True)

    _parse_arguments = field_validator("arguments", mode="before")(_parse_json_str)
    _parse_result = field_validator("result", mode="before")(_parse_json_str)


class TaskHistoryListResponse(BaseModel):
    items: list[TaskHistoryResponse]
    total: int


class TaskMethod(BaseModel):
    name: str
    description: str | None = None
    # The task's Pydantic ``model_json_schema()``; ``None`` = no arguments.
    arguments: dict[str, Any] | None = None
