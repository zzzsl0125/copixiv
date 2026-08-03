"""Use case: create a tag preference."""

from copixiv.domain.ports.repositories import TagRepository


class CreatePreferenceUseCase:
    """Create a new tag preference."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, data: dict) -> dict:
        return await self._repo.create_preference(data)
