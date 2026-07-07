"""Use case: count novels matching filters."""

from copixiv.infrastructure.repositories.novel import NovelRepository


class CountNovelsUseCase:
    """Count novels with the given filters."""

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(
        self,
        queries: dict[str, str] | None = None,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> int:
        return await self._repo.count_novels(
            queries=queries,
            min_like=min_like,
            min_text=min_text,
        )
