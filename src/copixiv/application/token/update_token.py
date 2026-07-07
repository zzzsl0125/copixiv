"""Use case: update a Pixiv token."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.token import TokenRepository


class UpdateTokenUseCase:
    """Update an existing token. Raises NotFoundError if not found."""

    def __init__(self, token_repo: TokenRepository):
        self._repo = token_repo

    async def execute(self, token_id: int, data: dict) -> dict:
        result = await self._repo.update(token_id, data)
        if not result:
            raise NotFoundError(f"Token {token_id} not found")
        return result
