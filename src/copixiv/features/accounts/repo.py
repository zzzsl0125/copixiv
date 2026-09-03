"""Token repository."""

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemyTokenRepository(BaseRepository):
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
        return self._update_attrs(models.Token, token_id, token_data)

    async def delete(self, token_id: int) -> bool:
        return self._delete_by_id(models.Token, token_id)

    async def reorder(self, ids: list[int]) -> bool:
        return self._reorder(models.Token, "sort_index", ids)

    async def set_follow(self, token_id: int, is_follow: bool) -> bool:
        """Designate one token as the「追更账号」, clearing the flag on all others.

        Returns ``False`` when *token_id* doesn't exist.  Runs as a single
        transaction — the write UoW commits on clean exit.  There is no
        global write lock under PG; the single transaction keeps the
        clear-then-set atomic.  At most one account can be flagged at a
        time.
        """
        if is_follow:
            self.session.execute(
                update(models.Token)
                .where(models.Token.id != token_id)
                .values(is_follow=False)
            )
        token = self.session.get(models.Token, token_id)
        if token is None:
            return False
        token.is_follow = is_follow
        return True
