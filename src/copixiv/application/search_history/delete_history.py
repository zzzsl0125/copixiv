"""Use case: delete a search history entry."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.search_history import SearchHistoryRepository


class DeleteHistoryUseCase:
    """Delete a search history entry by ID. Raises NotFoundError if not found."""

    def __init__(self, history_repo: SearchHistoryRepository):
        self._repo = history_repo

    async def execute(self, history_id: int) -> None:
        if not await self._repo.delete(history_id):
            raise NotFoundError(f"Search history {history_id} not found")
