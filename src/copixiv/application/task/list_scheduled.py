"""Use case: list scheduled tasks."""

from copixiv.domain.ports.repositories import TaskRepository


class ListScheduledUseCase:
    """Retrieve all scheduled tasks."""

    def __init__(self, task_repo: TaskRepository):
        self._repo = task_repo

    async def execute(self) -> list:
        return await self._repo.get_scheduled_tasks()
