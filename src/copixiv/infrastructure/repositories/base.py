"""Base repository — shared SQLAlchemy helpers."""

from typing import Any, TypeVar
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copixiv.infrastructure.database import constants as C

ModelType = TypeVar("ModelType", bound=Any)


def model_to_dict(obj: Any) -> dict:
    """Convert a single ORM instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def models_to_dicts(objs: list[Any]) -> list[dict]:
    """Convert a list of ORM instances to plain dicts."""
    return [model_to_dict(o) for o in objs]


def update_summary(
    session: Session,
    model_class: type[ModelType],
    id_column_name: str,
    ids: set[int] | int | None = None,
    extra_columns: list | None = None,
) -> None:
    """Update aggregate stats (count/views/likes/texts) on author/series rows.

    Queries the Novel table grouped by *id_column_name* and upserts the
    computed aggregates into *model_class*.  This is a standalone helper
    used by :class:`SQLAlchemyAuthorRepository` and
    :class:`SQLAlchemySeriesRepository` — it
    does not belong in ``BaseRepository`` because it hardcodes ``Novel``
    as the source table.
    """
    from ..database import models

    if ids is None:
        return
    if isinstance(ids, int):
        ids = {ids}
    if len(ids) == 0:
        return

    pk_col = getattr(model_class, id_column_name)

    columns_to_select = [
        getattr(models.Novel, id_column_name),
        func.count(models.Novel.id).label(C.COL_NOVEL_COUNT),
        func.sum(models.Novel.view).label(C.COL_VIEWS),
        func.sum(models.Novel.like).label(C.COL_LIKES),
        func.sum(models.Novel.text).label(C.COL_TEXTS),
    ]
    if extra_columns:
        columns_to_select.extend(extra_columns)

    select_stmt = (
        select(*columns_to_select)
        .where(getattr(models.Novel, id_column_name).in_(ids))
        .group_by(getattr(models.Novel, id_column_name))
    )

    results = session.execute(select_stmt).all()

    for row in results:
        mapping = row._mapping
        target_id = mapping[id_column_name]
        if target_id is None:
            continue

        values = {
            C.COL_NOVEL_COUNT: mapping[C.COL_NOVEL_COUNT],
            C.COL_VIEWS: mapping[C.COL_VIEWS],
            C.COL_LIKES: mapping[C.COL_LIKES],
            C.COL_TEXTS: mapping[C.COL_TEXTS],
        }

        for key, val in mapping.items():
            if key != id_column_name and key not in values and val is not None:
                values[key] = val

        insert_values = {pk_col.name: target_id, **values}
        stmt = (
            sqlite_insert(model_class)
            .values(insert_values)
            .on_conflict_do_update([pk_col], set_=values)
        )
        session.execute(stmt)


class BaseRepository:
    """Common database operations shared by all repositories."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, model_class: type[ModelType], item_id: Any) -> Any:
        return self.session.get(model_class, item_id)

    def get_all(
        self, model_class: type[ModelType], limit: int = 100, offset: int = 0
    ) -> list[Any]:
        stmt = select(model_class).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def count(self, model_class: type[ModelType]) -> int:
        stmt = select(func.count()).select_from(model_class)
        return self.session.execute(stmt).scalar()

    def get_summary_item(
        self, model_class: type[ModelType], item_id: int
    ) -> dict | None:
        pk_col = list(model_class.__mapper__.primary_key)[0]
        stmt = select(model_class).where(pk_col == item_id)
        result = self.session.execute(stmt).scalar_one_or_none()
        return model_to_dict(result) if result else None

    # -- shared CRUD helpers (used by token/task/tag/search_history) ----------

    def _update_attrs(
        self, model_class: type[ModelType], item_id: Any, data: dict
    ) -> Any | None:
        """Set the given attributes on an existing row.

        Returns the updated instance, or ``None`` when the row doesn't exist.
        ``None`` values are skipped (callers pass ``exclude_none`` data).
        """
        obj = self.session.get(model_class, item_id)
        if obj is None:
            return None
        for k, v in data.items():
            if v is not None and hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def _delete_by_id(self, model_class: type[ModelType], item_id: Any) -> bool:
        """Delete a row by id; returns False when it doesn't exist."""
        obj = self.session.get(model_class, item_id)
        if obj is None:
            return False
        self.session.delete(obj)
        return True

    def _reorder(
        self, model_class: type[ModelType], sort_field: str, ids: list[int]
    ) -> int:
        """Assign sort indices 0..n-1 to the rows in *ids* order.

        Afterwards every row of the table gets a dense re-index (rows not
        listed keep their relative order), so no duplicate / gapped
        sort_index values can accumulate.  Returns the number of listed
        rows that were actually found and updated.
        """
        matched = 0
        for idx, obj_id in enumerate(ids):
            obj = self.session.get(model_class, obj_id)
            if obj is not None:
                setattr(obj, sort_field, idx)
                matched += 1

        # Flush so the dense re-index below reads the NEW values (the
        # SELECT would otherwise order by the stale pre-update indices).
        self.session.flush()

        # Dense re-index: keep the (new) order, close any gaps left by
        # rows that kept stale indices.
        remaining = self.session.execute(
            select(model_class).order_by(
                getattr(model_class, sort_field), model_class.id
            )
        ).scalars().all()
        for idx, obj in enumerate(remaining):
            setattr(obj, sort_field, idx)
        return matched
