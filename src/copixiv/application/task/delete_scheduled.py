"""Use case: delete a scheduled task."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.ports.repositories import TaskRepository


class DeleteScheduledUseCase:
    """Delete a scheduled task and reload the cron scheduler.

    Raises NotFoundError if the task doesn't exist.
    """

    def __init__(self, task_repo: TaskRepository, task_manager=None):
        self._repo = task_repo
        self._task_manager = task_manager

    async def execute(self, task_id: int) -> None:
        if not await self._repo.delete_scheduled(task_id):
            raise NotFoundError(f"Task {task_id} not found")
        if self._task_manager:
            self._task_manager.reload_cron_jobs()
