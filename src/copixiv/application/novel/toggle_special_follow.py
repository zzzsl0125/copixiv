"""Use case: toggle special-follow status for an author."""

from copixiv.infrastructure.repositories.novel import NovelRepository


class ToggleSpecialFollowUseCase:
    """Toggle an author's special-follow status."""

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(self, author_id: int) -> None:
        await self._repo.toggle_special_follow(author_id)
