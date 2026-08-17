"""Notifier port — sending task results to external channels."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from copixiv.domain.models.task_result import TaskResult


@runtime_checkable
class NotifierPort(Protocol):
    """Port for sending notifications (Telegram, etc.).

    The *result* carries structured information about what the task did.
    Implementations decide how to format the message based on whether
    ``result.new_novel_titles`` is populated (novel-discovery task) or
    not (maintenance / summary-only task).
    """

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        result: TaskResult | None = None,
    ) -> None: ...


@runtime_checkable
class NotifierBackendPort(NotifierPort, Protocol):
    """A pluggable notification channel (docs/MODULARITY.md §M6).

    Backends are self-describing modules — a name, a build factory, and a
    lifecycle hook — so adding a channel means a new module plus one line
    of config (``notifiers.enabled``).
    """

    name: str

    async def close(self) -> None: ...
