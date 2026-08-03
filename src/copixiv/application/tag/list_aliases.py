"""Use case: list tag aliases."""

from copixiv.domain.ports.repositories import TagRepository


class ListAliasesUseCase:
    """Retrieve all tag aliases."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self) -> list:
        return await self._repo.get_aliases()
