"""Author repository."""

from datetime import datetime, timezone

from sqlalchemy import update as _update, delete as _delete, select as _select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db import constants as C
from copixiv.db.base import BaseRepository, model_to_dict, update_summary


class SQLAlchemyAuthorRepository(BaseRepository):
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, author_ids: set[int]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING placeholder rows (FK safety)."""
        if not author_ids:
            return
        for aid in author_ids:
            self.session.execute(
                pg_insert(models.Author)
                .values(author_id=aid)
                .on_conflict_do_nothing()
            )
        self.session.flush()

    def get_by_id(self, author_id: int) -> dict | None:
        author = self.session.get(models.Author, author_id)
        if author is None:
            return None
        return model_to_dict(author)

    def get_names_by_ids(self, author_ids: set[int]) -> dict[int, str]:
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

    def count_novels_by_author(self, author_id: int) -> int:
        """How many novels in the library currently point at *author_id*.

        Used by the editor's author-id preview: "库内已有 12 本" is how the
        user confirms the id they typed is the author they meant (the ``author``
        row's own ``novel_count`` is an aggregate refreshed by the ingest
        pipeline, not a live count).  Served by ``ix_novel_author_id``.
        """
        return int(self.session.execute(
            _select(func.count())
            .select_from(models.Novel)
            .where(models.Novel.author_id == author_id)
        ).scalar_one())

    def need_update(self, author_id: int) -> bool:
        author = self.session.get(models.Author, author_id)
        if author and author.last_update:
            last = author.last_update
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last).days > 0
        return True

    def update_summary(self, author_ids: set[int] | None = None) -> None:
        """Recalculate author aggregates."""
        self.update_summary_sync(author_ids)

    def update_summary_sync(self, author_ids: set[int] | None = None) -> None:
        """Same as :meth:`update_summary`, under the explicit sync name.

        For call sites that are already inside a transaction on this session —
        a delete path must keep its work on its own connection.  The
        statements live here once; :meth:`update_summary` delegates here.
        """
        update_summary(
            self.session, models.Author, C.COL_AUTHOR_ID, author_ids,
            extra_columns=[
                func.max(models.Novel.author_name).label(C.COL_AUTHOR_NAME)
            ],
        )

    def update_author_name(self, author_id: int, name: str) -> None:
        """Entry point — see :meth:`update_author_name_sync`."""
        self.update_author_name_sync(author_id, name)

    def update_author_name_sync(self, author_id: int, name: str) -> None:
        """Write *name* to the author row **and** normalise its novel rows.

        同步孪生体：语句与谓词只有这一份（``update_author_name`` 只是既有调用点
        沿用的名字，它委托到这里；本方法体内没有任何 await），给**同步的写路径**用：
        ``features/library/ingest.py`` 的上传管线是同步的（SQLAlchemy 的
        ``Session`` 本来就是同步 API），它要在写事务里做同一个收尾。

        Only touch novel rows whose name actually changed.  The
        ``sync_tag_refs`` trigger is declared ``UPDATE OF tags``, so an
        author-name writeback must not produce any ``novel`` UPDATE that
        could interact with ``tag`` row locks; narrowing the predicate
        also keeps the write batch short (no no-op row updates).
        """
        self.session.execute(
            _update(models.Novel)
            .where(
                models.Novel.author_id == author_id,
                models.Novel.author_name.is_distinct_from(name),
            )
            .values(author_name=name)
        )
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(author_name=name)
        )

    def update_last_update(self, author_id: int) -> None:
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(last_update=datetime.now(timezone.utc))
        )

    def delete_author_and_data(self, author_id: int) -> None:
        novel_ids = self.session.execute(
            _select(models.Novel.id).where(models.Novel.author_id == author_id)
        ).scalars().all()

        if novel_ids:
            # Deleting the novels cascades to failed_novel's explicit cleanup
            # below and the sync_tag_refs trigger decrements tag counts; the
            # search index follows automatically (expression index on novel).
            self.session.execute(
                _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
            )

        self.session.execute(
            _delete(models.Series).where(models.Series.author_id == author_id)
        )
        self.session.execute(
            _delete(models.Author).where(models.Author.author_id == author_id)
        )

    def get_empty_author_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Author.author_id)
            .where(models.Author.author_name.is_(None))
            .distinct()
        ).scalars().all())

    def get_special_follow_author_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Author.author_id).where(
                models.Author.is_special_follow == True
            )
        ).scalars().all())
