"""Use case: update a tag preference."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.tag import TagRepository


class UpdatePreferenceUseCase:
    """Update an existing tag preference. Raises NotFoundError if not found."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, pref_id: int, data: dict) -> dict:
        result = await self._repo.update_preference(pref_id, data)
        if not result:
            raise NotFoundError(f"Tag preference {pref_id} not found")
        return result
