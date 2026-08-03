"""Use case: list search history entries."""

from copixiv.domain.ports.repositories import SearchHistoryRepository


class ListHistoryUseCase:
    """Retrieve search history with pagination."""

    def __init__(self, history_repo: SearchHistoryRepository):
        self._repo = history_repo

    async def execute(self, limit: int = 50, offset: int = 0) -> list:
        return await self._repo.get_all(limit=limit, offset=offset)
