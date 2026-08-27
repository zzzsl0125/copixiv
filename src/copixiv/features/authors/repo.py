"""Author repository."""

import asyncio
from datetime import datetime, date

from sqlalchemy import update as _update, delete as _delete, select as _select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db import constants as C
from copixiv.db.base import BaseRepository, model_to_dict, update_summary
from copixiv.features.novels.fts import FTSManager


class SQLAlchemyAuthorRepository(BaseRepository):
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, author_ids: set[int]) -> None:
        """INSERT OR IGNORE placeholder rows so FK constraints are satisfied."""
        if not author_ids:
            return
        for aid in author_ids:
            self.session.execute(
                sqlite_insert(models.Author)
                .values(author_id=aid)
                .on_conflict_do_nothing()
            )
        self.session.flush()

    async def get_by_id(self, author_id: int) -> dict | None:
        author = self.session.get(models.Author, author_id)
        if author is None:
            return None
        return model_to_dict(author)

    async def get_names_by_ids(self, author_ids: set[int]) -> dict[int, str]:
        """Batch lookup: return ``{author_id: author_name}`` for ids whose
        name is already known (non-NULL).  Authors with ``author_name IS NULL``
        are silently omitted."""
        if not author_ids:
            return {}
        rows = self.session.execute(
            _select(models.Author.author_id, models.Author.author_name)
            .where(
                models.Author.author_id.in_(author_ids),
                models.Author.author_name.isnot(None),
            )
        ).fetchall()
        return {row.author_id: row.author_name for row in rows}

    async def need_update(self, author_id: int) -> bool:
        author = self.session.get(models.Author, author_id)
        if author and author.last_update:
            last = datetime.strptime(author.last_update, "%Y-%m-%d").date()
            return (date.today() - last).days > 0
        return True

    async def update_summary(self, author_ids: set[int] | None = None) -> None:
        """Recalculate author aggregates (runs in a worker thread)."""
        await asyncio.to_thread(self._update_summary_sync, author_ids)

    def _update_summary_sync(self, author_ids: set[int] | None = None) -> None:
        update_summary(
            self.session, models.Author, C.COL_AUTHOR_ID, author_ids,
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
            # Decrement tag reference counts BEFORE deleting the links —
            # without this the denormalized counter drifts permanently.
            links = self.session.execute(
                _select(models.NovelTag.novel_id, models.Tag.name)
                .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                .where(models.NovelTag.novel_id.in_(novel_ids))
            ).all()
            tag_link_counts: dict[str, int] = {}
            for _nid, tag_name in links:
                tag_link_counts[tag_name] = tag_link_counts.get(tag_name, 0) + 1
            for tag_name, cnt in tag_link_counts.items():
                self.session.execute(
                    _update(models.Tag)
                    .where(models.Tag.name == tag_name)
                    .values(reference_count=models.Tag.reference_count - cnt)
                )

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
