"""Author repository."""

from datetime import UTC, datetime

from sqlalchemy import delete as _delete
from sqlalchemy import func
from sqlalchemy import select as _select
from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from copixiv.core.exceptions import NotFoundError
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import (
    BaseRepository,
    model_to_dict,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository


class SQLAlchemyAuthorRepository(BaseRepository):
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

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
            _select(models.Author.author_id, models.Author.author_name).where(
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
        return int(
            self.session.execute(
                _select(func.count()).select_from(models.Novel).where(models.Novel.author_id == author_id)
            ).scalar_one()
        )

    def was_updated_today(self, author_id: int) -> bool:
        author = self.session.get(models.Author, author_id)
        if author and author.last_update:
            return (datetime.now(UTC) - author.last_update).days <= 0
        return False

    def update_author_name(self, author_id: int, name: str) -> None:
        """Write *name* to the author row **and** normalise its novel rows.

        Only touch novel rows whose name actually changed.  The
        ``sync_tag_refs`` trigger is declared ``UPDATE OF tags``, so an
        author-name writeback must not produce any ``novel`` UPDATE that
        could interact with ``tag`` row locks; narrowing the predicate
        also keeps the write batch short (no no-op row updates).
        """
        changed_ids = list(
            self.session.execute(
                _update(models.Novel)
                .where(
                    models.Novel.author_id == author_id,
                    models.Novel.author_name.is_distinct_from(name),
                )
                .values(author_name=name)
                .returning(models.Novel.id)
            )
            .scalars()
            .all()
        )
        if changed_ids:
            engine_delta.record_novel_upserts(self.session, changed_ids)
        self.session.execute(
            _update(models.Author)
            .where(
                models.Author.author_id == author_id,
                models.Author.author_name.is_distinct_from(name),
            )
            .values(author_name=name)
        )

    def update_last_update(self, author_id: int) -> None:
        self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(last_update=datetime.now(UTC))
        )

    def delete_author_and_data(self, author_id: int) -> list[str]:
        rows = self.session.execute(
            _select(
                models.Novel.id,
                models.Novel.path,
                models.Novel.series_id,
                models.Novel.tags,
            ).where(models.Novel.author_id == author_id)
        ).all()
        novel_ids = [r[0] for r in rows]
        paths = [r[1] for r in rows if r[1]]
        affected_series_ids = {r[2] for r in rows if r[2] is not None}
        affected_tags = {t for r in rows for t in (r[3] or [])}

        owned_series_ids = set(
            self.session.execute(_select(models.Series.series_id).where(models.Series.author_id == author_id))
            .scalars()
            .all()
        )

        if novel_ids:
            # failed_novel has no FK to novel; clear ledger rows explicitly.
            self.session.execute(
                _delete(models.FailedNovel).where(models.FailedNovel.novel_id.in_(novel_ids))
            )
            self.session.execute(_delete(models.Novel).where(models.Novel.id.in_(novel_ids)))
            engine_delta.record_novel_deletes(self.session, novel_ids)
            self.session.flush()
            if affected_tags:
                engine_delta.record_tags_dirty(self.session, affected_tags)
                SQLAlchemyTagRepository(self.session).prune_unused_tags(affected_tags)
        engine_delta.record_authors_dirty(self.session, [author_id])

        check_series = affected_series_ids | owned_series_ids
        if check_series:
            non_empty_series = set(
                self.session.execute(
                    _select(models.Novel.series_id).where(models.Novel.series_id.in_(check_series)).distinct()
                )
                .scalars()
                .all()
            )
            empty_series = check_series - non_empty_series
            if empty_series:
                self.session.execute(_delete(models.Series).where(models.Series.series_id.in_(empty_series)))
        else:
            non_empty_series = set()

        self.session.execute(_delete(models.Author).where(models.Author.author_id == author_id))
        if non_empty_series:
            remaining_author_subq = (
                _select(func.max(models.Novel.author_id))
                .where(models.Novel.series_id == models.Series.series_id)
                .scalar_subquery()
            )
            self.session.execute(
                _update(models.Series)
                .where(
                    models.Series.series_id.in_(non_empty_series),
                    models.Series.author_id.is_(None),
                )
                .values(author_id=remaining_author_subq)
            )
        return paths

    def get_special_follow_author_ids(self) -> list[int]:
        return list(
            self.session.execute(
                _select(models.Author.author_id).where(models.Author.is_special_follow == True)
            )
            .scalars()
            .all()
        )

    def toggle_special_follow(self, author_id: int) -> None:
        result = self.session.execute(
            _update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(is_special_follow=~models.Author.is_special_follow)
            .returning(models.Author.is_special_follow)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Author {author_id} not found")
        engine_delta.record_authors_dirty(self.session, [author_id])

    def get_novel_author_ids_with_empty_name(self) -> list[int]:
        """Return distinct non-null ``author_id`` values on novels whose ``author_name`` is NULL."""
        return [
            int(aid)
            for aid in self.session.execute(
                _select(models.Novel.author_id)
                .where(
                    models.Novel.author_name.is_(None),
                    models.Novel.author_id.isnot(None),
                )
                .distinct()
            )
            .scalars()
            .all()
            if aid is not None
        ]

    def count_novels_with_empty_author_name(self) -> int:
        """Return total number of novels whose ``author_name`` is NULL."""
        return int(
            self.session.execute(
                _select(func.count()).select_from(models.Novel).where(models.Novel.author_name.is_(None))
            ).scalar_one()
        )
