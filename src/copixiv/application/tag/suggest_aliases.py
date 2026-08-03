"""Use case: suggest tag alias mappings."""

from copixiv.domain.ports.repositories import TagRepository


class SuggestAliasesUseCase:
    """Find tags with similar names and suggest alias mappings."""

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(
        self,
        limit: int = 5,
        offset: int = 0,
        target_tag: str | None = None,
    ) -> dict:
        return await self._repo.suggest_aliases(
            limit=limit, offset=offset, target_tag=target_tag,
        )
