"""Shared SQLAlchemy repository helpers."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from copixiv.db import models

__all__ = [
    "delete_by_id",
    "ensure_parent_rows",
    "model_to_dict",
    "reorder_rows",
    "update_attrs",
]


def model_to_dict(obj: Any) -> dict:
    """Convert a single ORM instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def ensure_parent_rows(
    session: Session,
    *,
    author_ids: Iterable[int | None] = (),
    series_ids: Iterable[int | None] = (),
) -> None:
    """INSERT ... ON CONFLICT DO NOTHING Pixiv placeholder rows for positive FK safety."""
    a_ids = sorted({aid for aid in author_ids if aid is not None and aid > 0})
    s_ids = sorted({sid for sid in series_ids if sid is not None and sid > 0})
    if a_ids:
        session.execute(
            postgresql_insert(models.Author)
            .values([{"author_id": aid, "source": "pixiv", "source_key": str(aid)} for aid in a_ids])
            .on_conflict_do_nothing()
        )
    if s_ids:
        session.execute(
            postgresql_insert(models.Series)
            .values([{"series_id": sid, "source": "pixiv", "source_key": str(sid)} for sid in s_ids])
            .on_conflict_do_nothing()
        )


def update_attrs[ModelType: Any](
    session: Session,
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
    obj = session.get(model_class, item_id)
    if obj is None:
        return None
    nullable = set(allow_none_fields)
    for k, v in data.items():
        if (v is not None or k in nullable) and hasattr(obj, k):
            setattr(obj, k, v)
    return obj


def delete_by_id[ModelType: Any](session: Session, model_class: type[ModelType], item_id: Any) -> bool:
    """Delete a row by id; returns False when it doesn't exist."""
    obj = session.get(model_class, item_id)
    if obj is None:
        return False
    session.delete(obj)
    session.flush()
    return True


def reorder_rows[ModelType: Any](
    session: Session,
    model_class: type[ModelType],
    sort_field: str,
    ids: list[int],
) -> int:
    """Assign sort indices 0..n-1 with the listed *ids* placed first.

    Loads all rows in a single query, places valid *ids* in caller order
    (skipping duplicates), keeps remaining rows in their existing
    ``(sort_field, id)`` order, and assigns dense indices. Returns the
    number of listed rows that were found.
    """
    rows = (
        session.execute(select(model_class).order_by(getattr(model_class, sort_field), model_class.id))
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
