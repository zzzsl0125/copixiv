"""Author repository."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, case, func, or_, tuple_
from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from copixiv.core.exceptions import NotFoundError
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import model_to_dict

_AUTHOR_UPDATE_COOLDOWN = timedelta(hours=23)
_SIGNED_INT_RE = re.compile(r"^-?\d+$")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _parse_signed_int64(text: str) -> int | None:
    if not text or not _SIGNED_INT_RE.fullmatch(text):
        return None
    value = int(text)
    return value if _INT64_MIN <= value <= _INT64_MAX else None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SQLAlchemyAuthorRepository:
    """Repository for author CRUD and statistics."""

    def __init__(self, session: Session):
        self.session = session

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
            return (datetime.now(UTC) - author.last_update) < _AUTHOR_UPDATE_COOLDOWN
        return False

    def update_author_name(self, author_id: int, name: str) -> None:
        """Write *name* to the author row **and** normalise its novel rows.

        Only touch novel rows whose name actually changed, keeping the
        write batch short (no no-op row updates).
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
        owned_series_ids = [
            int(sid)
            for sid in self.session.execute(
                _select(models.Series.series_id).where(models.Series.author_id == author_id)
            )
            .scalars()
            .all()
        ]
        if owned_series_ids:
            engine_delta.record_series_dirty(self.session, owned_series_ids)
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
                models.Novel.source,
                models.Novel.source_key,
            ).where(models.Novel.author_id == author_id)
        ).all()
        novel_ids = [r[0] for r in rows]
        paths = [r[1] for r in rows if r[1]]
        affected_series_ids = {r[2] for r in rows if r[2] is not None}
        source_key_pairs = [(r[3], r[4]) for r in rows if r[3] and r[4]]

        owned_series_ids = set(
            self.session.execute(_select(models.Series.series_id).where(models.Series.author_id == author_id))
            .scalars()
            .all()
        )

        if novel_ids:
            # failed_novel has no FK to novel; clear ledger rows explicitly.
            failure_pred: ColumnElement[bool] = models.FailedNovel.novel_id.in_(novel_ids)
            if source_key_pairs:
                failure_pred = or_(
                    failure_pred,
                    tuple_(models.FailedNovel.source, models.FailedNovel.source_key).in_(source_key_pairs),
                )
            self.session.execute(_delete(models.FailedNovel).where(failure_pred))
            self.session.execute(_delete(models.Novel).where(models.Novel.id.in_(novel_ids)))
            engine_delta.record_novel_deletes(self.session, novel_ids)
            self.session.flush()
        engine_delta.record_authors_dirty(self.session, [author_id])

        check_series = affected_series_ids | owned_series_ids
        if check_series:
            engine_delta.record_series_dirty(self.session, check_series)
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

    def get_special_follow_authors_by_source(self) -> dict[str, list[str]]:
        """Return ``{source: [source_key, ...]}`` for specially-followed authors."""
        rows = self.session.execute(
            _select(models.Author.source, models.Author.source_key)
            .where(models.Author.is_special_follow == True)
            .order_by(models.Author.source, models.Author.author_id)
        ).all()
        grouped: dict[str, list[str]] = {}
        for source, source_key in rows:
            if source and source_key:
                grouped.setdefault(str(source), []).append(str(source_key))
        return grouped

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

    def search_authors(
        self,
        q: str = "",
        *,
        source: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search authors by exact ID or name substring, ordered by relevance and novel count."""
        capped_limit = max(1, min(int(limit), 50))
        cleaned = (q or "").strip()
        parsed_id = _parse_signed_int64(cleaned)
        novel_count_expr = func.count(models.Novel.id).label("novel_count")

        stmt = (
            _select(
                models.Author.author_id,
                models.Author.author_name,
                models.Author.source,
                novel_count_expr,
            )
            .outerjoin(models.Novel, models.Novel.author_id == models.Author.author_id)
            .group_by(
                models.Author.author_id,
                models.Author.author_name,
                models.Author.source,
            )
        )

        if source is not None:
            cleaned_source = source.strip()
            if cleaned_source:
                stmt = stmt.where(models.Author.source == cleaned_source)

        if cleaned:
            pattern = f"%{_escape_like(cleaned)}%"
            name_match = models.Author.author_name.ilike(pattern, escape="\\")
            if parsed_id is not None:
                stmt = stmt.where(or_(models.Author.author_id == parsed_id, name_match))
            else:
                stmt = stmt.where(name_match)

        order_by: list[Any] = []
        if parsed_id is not None:
            order_by.append(case((models.Author.author_id == parsed_id, 0), else_=1))
        order_by.extend([novel_count_expr.desc(), models.Author.author_id.asc()])

        rows = self.session.execute(stmt.order_by(*order_by).limit(capped_limit)).all()
        return [
            {
                "author_id": int(row.author_id),
                "author_name": row.author_name,
                "source": str(row.source),
                "novel_count": int(row.novel_count or 0),
            }
            for row in rows
        ]
