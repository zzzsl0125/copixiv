"""Base repository — shared SQLAlchemy helpers."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from copixiv.db import models


def model_to_dict(obj: Any) -> dict:
    """Convert a single ORM instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def ensure_parent_rows(
    session: Session,
    *,
    author_ids: Iterable[int | None] = (),
    series_ids: Iterable[int | None] = (),
) -> None:
    """INSERT ... ON CONFLICT DO NOTHING placeholder rows for FK safety."""
    a_ids = sorted({aid for aid in author_ids if aid})
    s_ids = sorted({sid for sid in series_ids if sid})
    if a_ids:
        session.execute(
            postgresql_insert(models.Author)
            .values([{"author_id": aid} for aid in a_ids])
            .on_conflict_do_nothing()
        )
    if s_ids:
        session.execute(
            postgresql_insert(models.Series)
            .values([{"series_id": sid} for sid in s_ids])
            .on_conflict_do_nothing()
        )


class BaseRepository:
    """Common database operations shared by all repositories."""

    def __init__(self, session: Session):
        self.session = session

    def count[ModelType: Any](self, model_class: type[ModelType]) -> int:
        stmt = select(func.count()).select_from(model_class)
        return self.session.execute(stmt).scalar() or 0

    # -- shared CRUD helpers (used by token/task/tag/search_history) ----------

    def _update_attrs[ModelType: Any](
        self,
        model_class: type[ModelType],
        item_id: Any,
        data: dict,
        *,
        allow_none_fields: Iterable[str] = (),
    ) -> ModelType | None:
        """Set the given attributes on an existing row.

        Returns the updated instance, or ``None`` when the row doesn't exist.
        ``None`` values are skipped unless the field name is listed in
        *allow_none_fields*.
        """
        obj = self.session.get(model_class, item_id)
        if obj is None:
            return None
        nullable = set(allow_none_fields)
        for k, v in data.items():
            if (v is not None or k in nullable) and hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def _delete_by_id[ModelType: Any](self, model_class: type[ModelType], item_id: Any) -> bool:
        """Delete a row by id; returns False when it doesn't exist."""
        obj = self.session.get(model_class, item_id)
        if obj is None:
            return False
        self.session.delete(obj)
        self.session.flush()
        return True

    def _reorder[ModelType: Any](self, model_class: type[ModelType], sort_field: str, ids: list[int]) -> int:
        """Assign sort indices 0..n-1 with the listed *ids* placed first.

        Loads all rows in a single query, places valid *ids* in caller order
        (skipping duplicates), keeps remaining rows in their existing
        ``(sort_field, id)`` order, and assigns dense indices. Returns the
        number of listed rows that were found.
        """
        rows = (
            self.session.execute(
                select(model_class).order_by(getattr(model_class, sort_field), model_class.id)
            )
            .scalars()
            .all()
        )
        by_id = {obj.id: obj for obj in rows}
        ordered: list[Any] = []
        seen: set[int] = set()
        matched = 0
        for obj_id in ids:
            if obj_id in seen:
                continue
            seen.add(obj_id)
            obj = by_id.get(obj_id)
            if obj is not None:
                ordered.append(obj)
                matched += 1
        for obj in rows:
            if obj.id not in seen:
                ordered.append(obj)
        for idx, obj in enumerate(ordered):
            setattr(obj, sort_field, idx)
        return matched
