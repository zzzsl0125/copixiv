"""Use case: get task execution history."""

from copixiv.domain.ports.repositories import TaskRepository


class GetHistoryUseCase:
    """Retrieve task execution history with pagination."""

    def __init__(self, task_repo: TaskRepository):
        self._repo = task_repo

    async def execute(self, limit: int = 50, offset: int = 0) -> dict:
        history = await self._repo.get_history(limit=limit, offset=offset)
        total = await self._repo.count_history()
        return {"items": history, "total": total}
