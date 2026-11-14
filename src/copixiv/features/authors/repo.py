"""Author repository."""

from datetime import datetime, timezone

from sqlalchemy import delete as _delete
from sqlalchemy import func
from sqlalchemy import select as _select
from sqlalchemy import update as _update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import constants as C
from copixiv.db import models
from copixiv.db.base import (
    BaseRepository,
    model_to_dict,
    refresh_parent_summaries,
    update_summary,
)


class SQLAlchemyAuthorRepository(BaseRepository):
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, author_ids: set[int]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING placeholder rows (FK safety)."""
        if not author_ids:
            return
        self.session.execute(
            pg_insert(models.Author)
            .values([{"author_id": aid} for aid in sorted(author_ids)])
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
        update_summary(
            self.session, models.Author, C.COL_AUTHOR_ID, author_ids,
            extra_columns=[
                func.max(models.Novel.author_name).label(C.COL_AUTHOR_NAME)
            ],
        )

    def update_author_name(self, author_id: int, name: str) -> None:
        """Write *name* to the author row **and** normalise its novel rows.

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

    def delete_author_and_data(self, author_id: int) -> list[str]:
        rows = self.session.execute(
            _select(models.Novel.id, models.Novel.path, models.Novel.series_id)
            .where(models.Novel.author_id == author_id)
        ).all()
        novel_ids = [r[0] for r in rows]
        paths = [r[1] for r in rows if r[1]]
        affected_series_ids = {r[2] for r in rows if r[2] is not None}

        owned_series_ids = set(
            self.session.execute(
                _select(models.Series.series_id)
                .where(models.Series.author_id == author_id)
            ).scalars().all()
        )

        if novel_ids:
            # failed_novel has no FK to novel; clear ledger rows explicitly.
            self.session.execute(
                _delete(models.FailedNovel)
                .where(models.FailedNovel.novel_id.in_(novel_ids))
            )
            self.session.execute(
                _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
            )

        check_series = affected_series_ids | owned_series_ids
        if check_series:
            non_empty_series = set(
                self.session.execute(
                    _select(models.Novel.series_id)
                    .where(models.Novel.series_id.in_(check_series))
                    .distinct()
                ).scalars().all()
            )
            empty_series = check_series - non_empty_series
            if empty_series:
                self.session.execute(
                    _delete(models.Series)
                    .where(models.Series.series_id.in_(empty_series))
                )
            if non_empty_series:
                # Detach deleted author before deleting the author row and
                # recompute summary aggregates for remaining shared series.
                self.session.execute(
                    _update(models.Series)
                    .where(
                        models.Series.series_id.in_(non_empty_series),
                        models.Series.author_id == author_id,
                    )
                    .values(author_id=None)
                )
                refresh_parent_summaries(self.session, series_ids=non_empty_series)

        self.session.execute(
            _delete(models.Author).where(models.Author.author_id == author_id)
        )
        return paths

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
