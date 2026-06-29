"""Token repository."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from .base import BaseRepository


class TokenRepository(BaseRepository):
    """Repository for Pixiv refresh tokens."""

    def __init__(self, session: Session):
        super().__init__(session)

    async def get_all(self) -> Sequence[models.Token]:
        stmt = select(models.Token).order_by(models.Token.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    async def get_by_name(self, name: str) -> models.Token | None:
        stmt = select(models.Token).where(models.Token.name == name)
        return self.session.execute(stmt).scalar_one_or_none()

    async def create(self, token_data: dict) -> models.Token:
        token = models.Token(**token_data)
        self.session.add(token)
        self.session.flush()
        return token

    async def update(self, token_id: int, token_data: dict) -> models.Token | None:
        token = self.session.get(models.Token, token_id)
        if token is None:
            return None
        for k, v in token_data.items():
            if hasattr(token, k):
                setattr(token, k, v)
        return token

    async def delete(self, token_id: int) -> bool:
        token = self.session.get(models.Token, token_id)
        if token is None:
            return False
        self.session.delete(token)
        return True
