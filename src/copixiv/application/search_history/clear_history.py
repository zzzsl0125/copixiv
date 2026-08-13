"""Use case: clear all search history entries."""

from copixiv.domain.ports.repositories import SearchHistoryRepository


class ClearHistoryUseCase:
    """Remove every search-history row.

    Backs the frontend's "全部清除" button (``DELETE /search-history/``),
    which previously had no backend route and failed with 405.
    """

    def __init__(self, history_repo: SearchHistoryRepository):
        self._repo = history_repo

    async def execute(self) -> int:
        return await self._repo.clear_all()
