"""Use case: create a tag alias with retroactive application."""

from copixiv.domain.exceptions import ValidationError
from copixiv.domain.ports.repositories import TagRepository


class CreateAliasUseCase:
    """Create a tag alias and apply it retroactively to existing novels.

    Validates that source != target, then creates the alias mapping and
    applies it to all existing tag assignments.
    """

    def __init__(self, tag_repo: TagRepository):
        self._repo = tag_repo

    async def execute(self, data: dict) -> dict:
        source = data.get("source", "")
        target = data.get("target", "")
        if source == target:
            raise ValidationError("原标签不能和目标标签相同")

        alias = await self._repo.create_alias(data)
        await self._repo.apply_alias_retroactively(source, target)
        return alias
