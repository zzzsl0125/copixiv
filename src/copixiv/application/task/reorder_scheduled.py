"""Use case: reorder scheduled tasks."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.task import TaskRepository


class ReorderScheduledUseCase:
    """Reorder scheduled tasks and reload the cron scheduler.

    Raises NotFoundError if any task wasn't found.
    """

    def __init__(self, task_repo: TaskRepository, task_manager=None):
        self._repo = task_repo
        self._task_manager = task_manager

    async def execute(self, task_ids: list[int]) -> None:
        if not await self._repo.reorder_scheduled(task_ids):
            raise NotFoundError("Failed to reorder tasks")
        if self._task_manager:
            self._task_manager.reload_cron_jobs()
