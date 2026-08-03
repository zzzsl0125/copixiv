"""Use case: update a scheduled task."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.ports.repositories import TaskRepository


class UpdateScheduledUseCase:
    """Update an existing scheduled task and reload the cron scheduler.

    Raises NotFoundError if the task doesn't exist.
    """

    def __init__(self, task_repo: TaskRepository, task_manager=None):
        self._repo = task_repo
        self._task_manager = task_manager

    async def execute(self, task_id: int, data: dict) -> dict:
        task = await self._repo.update_scheduled(task_id, data)
        if not task:
            raise NotFoundError(f"Task {task_id} not found")
        if self._task_manager:
            self._task_manager.reload_cron_jobs()
        return task
