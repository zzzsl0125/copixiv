"""Notifier port — sending task results to external channels."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotifierPort(Protocol):
    """Port for sending notifications (Telegram, etc.)."""

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        new_novels_count: int = 0,
        new_novel_titles: list[str] | None = None,
    ) -> None: ...
