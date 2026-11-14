"""Task kernel facade — re-exports registry, executor, cron, and manager."""

from .cron import (
    CRON_MISFIRE_GRACE_SECONDS,
    CRON_TICK_SECONDS,
    CRON_TIMEZONE,
    CronTicker,
    next_occurrence,
)
from .executor import TaskContext, TaskExecutor, TaskHistoryRecorder
from .manager import TaskManagerSystem, _PendingTask
from .registry import (
    DEFAULT_TASK_MODULES,
    FETCH_TASK_NAMES,
    TaskSpec,
    describe_tasks,
    discover_tasks,
    get_spec,
    get_task,
    register,
)

__all__ = [
    "CRON_MISFIRE_GRACE_SECONDS",
    "CRON_TICK_SECONDS",
    "CRON_TIMEZONE",
    "CronTicker",
    "DEFAULT_TASK_MODULES",
    "FETCH_TASK_NAMES",
    "TaskContext",
    "TaskExecutor",
    "TaskHistoryRecorder",
    "TaskManagerSystem",
    "TaskSpec",
    "_PendingTask",
    "describe_tasks",
    "discover_tasks",
    "get_spec",
    "get_task",
    "next_occurrence",
    "register",
]
