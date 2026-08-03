"""Use case: list novels with filters, pagination, and search."""

from dataclasses import dataclass

from copixiv.domain.ports.repositories import NovelRepository


@dataclass
class ListNovelsRequest:
    queries: dict[str, str] | None = None
    order_by: str = "like"
    order_direction: str = "DESC"
    cursor: dict | None = None
    per_page: int = 20
    min_like: int | None = None
    min_text: int | None = None


class ListNovelsUseCase:
    """Retrieve a paginated, filtered list of novels.

    Also returns the parsed queries dict so the endpoint can schedule
    search-history recording in a background task.
    """

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(self, req: ListNovelsRequest) -> dict:
        return await self._repo.get_novels(
            queries=req.queries,
            order_by=req.order_by,
            order_direction=req.order_direction,
            cursor=req.cursor,
            per_page=req.per_page,
            min_like=req.min_like,
            min_text=req.min_text,
        )
