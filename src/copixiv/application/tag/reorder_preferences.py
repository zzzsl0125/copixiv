"""Use case: reorder tag preferences."""

from copixiv.infrastructure.repositories.tag import TagRepository


class ReorderPreferencesUseCase:
    """Reorder tag preferences by a list of IDs."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, ids: list[int]) -> None:
        await self._repo.reorder_preferences(ids)
