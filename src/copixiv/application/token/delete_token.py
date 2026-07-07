"""Use case: delete a Pixiv token."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.repositories.token import TokenRepository


class DeleteTokenUseCase:
    """Delete a token by ID. Raises NotFoundError if not found."""

    def __init__(self, token_repo: TokenRepository):
        self._repo = token_repo

    async def execute(self, token_id: int) -> None:
        if not await self._repo.delete(token_id):
            raise NotFoundError(f"Token {token_id} not found")
