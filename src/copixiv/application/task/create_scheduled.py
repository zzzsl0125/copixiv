"""Use case: create a scheduled task."""

from copixiv.infrastructure.repositories.task import TaskRepository


class CreateScheduledUseCase:
    """Create a new scheduled task and reload the cron scheduler."""

    def __init__(self, task_repo: TaskRepository, task_manager=None):
        self._repo = task_repo
        self._task_manager = task_manager

    async def execute(self, data: dict) -> dict:
        task = await self._repo.create_scheduled(data)
        if self._task_manager:
            self._task_manager.reload_cron_jobs()
        return task
