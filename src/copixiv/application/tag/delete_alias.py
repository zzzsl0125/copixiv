"""Use case: delete a tag alias."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.tag import TagRepository


class DeleteAliasUseCase:
    """Delete a tag alias by ID. Raises NotFoundError if not found."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, alias_id: int) -> None:
        if not await self._repo.delete_alias(alias_id):
            raise NotFoundError(f"Tag alias {alias_id} not found")
