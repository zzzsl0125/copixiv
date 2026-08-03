"""Use case: create a Pixiv token."""

from copixiv.domain.ports.repositories import TokenRepository


class CreateTokenUseCase:
    """Create a new Pixiv refresh token."""

    def __init__(self, token_repo: TokenRepository):
        self._repo = token_repo

    async def execute(self, data: dict) -> dict:
        return await self._repo.create(data)
