"""Use case: list tag preferences."""

from copixiv.infrastructure.repositories.tag import TagRepository


class ListPreferencesUseCase:
    """Retrieve all tag preferences ordered by sort_index."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self) -> list:
        return await self._repo.get_preferences()
