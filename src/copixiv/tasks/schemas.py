"""Task management API schemas — carried with the task feature (S1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Task Management
# ---------------------------------------------------------------------------


class ScheduledTaskBase(BaseModel):
    """Shared structural fields for scheduled task read/write models."""

    name: str
    task: str
    cron: str
    params: dict[str, Any] | None = None
    is_enabled: bool = False
    sort_index: int = 0


class ScheduledTaskCreate(ScheduledTaskBase):
    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        """Reject malformed cron expressions at the API boundary (422).

        Keeps the previous 5-field crontab contract: exactly five
        whitespace-separated fields (minute hour day month weekday).
        Validity itself is delegated to :func:`copixiv.tasks.cron.next_occurrence`
        (croniter), whose ``ValueError`` becomes a Pydantic 422.
        """

        from copixiv.tasks.cron import next_occurrence

        if len(v.split()) != 5:
            raise ValueError(
                "Invalid cron expression: exactly 5 fields are required "
                "(minute hour day-of-month month day-of-week)"
            )
        try:
            next_occurrence(v, datetime.now(UTC))
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


# Read response deliberately omits write-boundary validators so one legacy row
# with a malformed cron does not 500 the entire schedule list.
class ScheduledTaskResponse(ScheduledTaskBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class TaskHistoryResponse(BaseModel):
    id: int
    name: str
    task_func: str | None = None
    arguments: dict[str, Any] | None = None
    status: str
    start_time: datetime
    end_time: datetime | None = None
    duration: float | None = None
    result: dict[str, Any] | None = None
    has_log: bool = False
    progress: Any | None = None
    model_config = ConfigDict(from_attributes=True)


class TaskHistoryLogResponse(BaseModel):
    """The stored execution log for one task-history row."""

    id: int
    log: str


class TaskHistoryListResponse(BaseModel):
    items: list[TaskHistoryResponse]
    total: int


TaskArgType = Literal["string", "integer", "number", "boolean", "array"]


class TaskArgumentField(BaseModel):
    name: str
    type: TaskArgType
    required: bool = False
    nullable: bool = False
    default: Any = None
    description: str | None = None


class TaskMethod(BaseModel):
    name: str
    description: str | None = None
    arguments: list[TaskArgumentField] = []


class TaskCancelResponse(BaseModel):
    """Response for ``POST /api/tasks/history/{task_id}/cancel``."""

    task_id: int
    status: str
