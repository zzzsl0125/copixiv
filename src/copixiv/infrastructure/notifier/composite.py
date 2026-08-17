"""Composite notifier — fans task results out to every enabled backend.

Implements :class:`NotifierPort` so the task kernel stays unchanged:
one backend failing (network error, misconfigured URL) must never
affect the others, and ``close()`` releases every backend.
"""

from __future__ import annotations

from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.ports.notifier import NotifierBackendPort
from copixiv.log import logger


class CompositeNotifier:
    """Broadcasts notifications across a list of backends (docs §M6)."""

    def __init__(self, backends: list[NotifierBackendPort]):
        self._backends = list(backends)

    @property
    def backends(self) -> list[NotifierBackendPort]:
        return list(self._backends)

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        result: TaskResult | None = None,
    ) -> None:
        for backend in self._backends:
            try:
                await backend.send_task_result(
                    task_name=task_name,
                    status=status,
                    duration=duration,
                    error=error,
                    result=result,
                )
            except Exception:
                # One bad channel must never break the others (or the task).
                logger.exception(
                    "Notifier backend '%s' failed to send task result.",
                    getattr(backend, "name", backend.__class__.__name__),
                )

    async def close(self) -> None:
        for backend in self._backends:
            try:
                await backend.close()
            except Exception:
                logger.exception(
                    "Notifier backend '%s' failed to close.",
                    getattr(backend, "name", backend.__class__.__name__),
                )
