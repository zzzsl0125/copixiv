"""Notifier port — sending task results to external channels."""

from __future__ import annotations

from typing import Protocol

from copixiv.domain.models.task_result import TaskResult


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


class NotifierBackendPort(NotifierPort, Protocol):
    """A notification channel (docs/MODULARITY.md §M6).

    A name plus a lifecycle hook.  The two built-in backends
    (``telegram`` / ``webhook``) are selected from config by
    ``notifier.factory.build_notifiers`` — a plain mapping, not a
    plugin registry.
    """

    name: str

    async def close(self) -> None: ...
