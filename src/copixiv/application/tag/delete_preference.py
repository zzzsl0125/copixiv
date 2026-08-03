"""Use case: delete a tag preference."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.ports.repositories import TagRepository


class DeletePreferenceUseCase:
    """Delete a tag preference by ID. Raises NotFoundError if not found."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, pref_id: int) -> None:
        if not await self._repo.delete_preference(pref_id):
            raise NotFoundError(f"Tag preference {pref_id} not found")
