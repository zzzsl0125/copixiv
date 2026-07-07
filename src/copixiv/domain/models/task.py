"""Task domain entities."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskHistory(BaseModel):
    """Record of a completed or in-progress task execution."""

    id: int = 0
    name: str
    arguments: dict | None = None
    status: str = TaskStatus.PENDING
    start_time: datetime
    end_time: datetime | None = None
    duration: float | None = None
    result: dict | None = None


class ScheduledTask(BaseModel):
    """A task scheduled to run on a cron expression."""

    id: int = 0
    name: str
    task: str
    cron: str
    params: dict | None = None
    is_enabled: bool = False
    config: dict | None = None
    sort_index: int = 0
