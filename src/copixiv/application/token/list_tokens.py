"""Use case: list all Pixiv tokens."""

from copixiv.infrastructure.repositories.token import TokenRepository


class ListTokensUseCase:
    """Retrieve all tokens ordered by sort_index."""

    def __init__(self, token_repo: TokenRepository):
        self._repo = token_repo

    async def execute(self) -> list:
        return await self._repo.get_all()
