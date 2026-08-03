"""Use case: toggle novel favourite status."""

from copixiv.domain.ports.repositories import NovelRepository


class ToggleFavouriteUseCase:
    """Toggle a novel's favourite status."""

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(self, novel_id: int) -> None:
        await self._repo.toggle_favourite(novel_id)
