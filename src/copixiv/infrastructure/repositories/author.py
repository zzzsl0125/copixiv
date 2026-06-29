"""Author repository."""

from datetime import datetime, date

from sqlalchemy import update as _update, delete as _delete, select as _select, func
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from .base import BaseRepository
from .fts import FTSManager


class AuthorRepository(BaseRepository):
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    async def get_by_id(self, author_id: int) -> dict | None:
        author = self.session.get(models.Author, author_id)
        if author is None:
            return None
        return {c.name: getattr(author, c.name) for c in author.__table__.columns}

    async def need_update(self, author_id: int) -> bool:
        author = self.session.get(models.Author, author_id)
        if author and author.last_update:
            last = datetime.strptime(author.last_update, "%Y-%m-%d").date()
            return (date.today() - last).days > 0
        return True

    async def update_summary(self, author_ids: set[int] | None = None) -> None:
        super()._update_summary(
            models.Author, C.COL_AUTHOR_ID, author_ids,
            extra_columns=[
                func.max(models.Novel.author_name).label(C.COL_AUTHOR_NAME)
            ],
        )

    async def update_author_name(self, author_id: int, name: str) -> None:
        self.session.execute(
            _update(models.Novel)
            .where(models.Novel.author_id == author_id)
            .values(author_name=name)
        )
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(author_name=name)
        )

    async def update_last_update(self, author_id: int) -> None:
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(last_update=date.today().isoformat())
        )

    async def delete_author_and_data(self, author_id: int) -> None:
        novel_ids = self.session.execute(
            _select(models.Novel.id).where(models.Novel.author_id == author_id)
        ).scalars().all()

        if novel_ids:
            fts = FTSManager(self.session)
            for nid in novel_ids:
                fts.delete_novel_fts(nid)
            self.session.execute(
                _delete(models.NovelTag).where(
                    models.NovelTag.novel_id.in_(novel_ids)
                )
            )
            self.session.execute(
                _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
            )

        self.session.execute(
            _delete(models.Series).where(models.Series.author_id == author_id)
        )
        self.session.execute(
            _delete(models.Author).where(models.Author.author_id == author_id)
        )

    async def get_empty_author_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Author.author_id)
            .where(models.Author.author_name.is_(None))
            .distinct()
        ).scalars().all())

    async def get_special_follow_author_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.SpecialFollow.author_id)
        ).scalars().all())
