"""Use case: reorder Pixiv tokens."""

from copixiv.infrastructure.repositories.token import TokenRepository


class ReorderTokensUseCase:
    """Reorder tokens by a list of IDs."""

    def __init__(self, token_repo: TokenRepository):
        self._repo = token_repo

    async def execute(self, ids: list[int]) -> None:
        await self._repo.reorder(ids)
