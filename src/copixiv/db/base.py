"""Base repository — shared SQLAlchemy helpers."""

from typing import Any, TypeVar
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from copixiv.db import constants as C

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
    """Recompute the aggregate columns of *model_class* rows for *ids*.

    ``author`` / ``series`` carry rolled-up statistics over the novels that point
    at them (``novel_count`` / ``like`` / ``view`` / ``text``, plus the name
    derived from the novels themselves).  This function is the **only** place
    that turns ``novel`` rows into those numbers — the ingest pipeline
    (``features/novels/persist.py``), the editor's writeback and the delete
    paths all call it, so the arithmetic exists once.

    ``ids=None`` is a no-op (not "every id"): the callers always know which rows
    they just touched, and an accidental "refresh the whole table" is not
    something this helper should be able to do.

    **Call it after the write, in the same transaction, for every row whose
    novel set changed — including deletions.**  An aggregate that is only
    refreshed on insert drifts upward forever, and nothing else recomputes it:
    the weekly reconciler sweeps EPUB state, not author counters.

    ``extra_columns`` are additional aggregate expressions over ``Novel`` (the
    two repositories pass ``func.max(Novel.author_name)`` /
    ``func.max(Novel.series_name)`` to derive the display name from its novels);
    each must be labelled with the target column name.

    Rows with no novels left are **zeroed rather than deleted**: the row is
    still an entity (the user may re-attach a work to it, and the ``author`` /
    ``series`` foreign keys require it to exist), and a missing row would read
    as "unknown id" everywhere.
    """
    from . import models

    if ids is None:
        return
    if isinstance(ids, int):
        ids = {ids}
    if not ids:
        return

    pk_col = getattr(model_class, id_column_name)
    source_col = getattr(models.Novel, id_column_name)

    columns_to_select = [
        source_col,
        func.count(models.Novel.id).label(C.COL_NOVEL_COUNT),
        func.sum(models.Novel.view).label(C.COL_VIEWS),
        func.sum(models.Novel.like).label(C.COL_LIKES),
        func.sum(models.Novel.text).label(C.COL_TEXTS),
    ]
    if extra_columns:
        columns_to_select.extend(extra_columns)

    rows = session.execute(
        select(*columns_to_select).where(source_col.in_(ids)).group_by(source_col)
    ).all()

    for row in rows:
        mapping = row._mapping
        target_id = mapping[id_column_name]
        if target_id is None:
            # An orphaned ``novel`` row (no author / series) has nothing to
            # aggregate into — and NULL is not a legal key here.
            continue

        values = {
            C.COL_NOVEL_COUNT: mapping[C.COL_NOVEL_COUNT],
            C.COL_VIEWS: mapping[C.COL_VIEWS],
            C.COL_LIKES: mapping[C.COL_LIKES],
            C.COL_TEXTS: mapping[C.COL_TEXTS],
        }
        # Derived extras (``author_name`` / ``series_name``): a NULL ``max``
        # means no novel carries a name — leave the stored value rather than
        # blanking it on every refresh.
        values.update({
            key: val
            for key, val in mapping.items()
            if key != id_column_name and key not in values and val is not None
        })
        _upsert_summary_row(session, model_class, pk_col, target_id, values)

    # Ids with no novels left: zero their aggregates so a delete is reflected.
    # ``ids - grouped`` is computed in Python because every caller passes a small
    # set (one batch's authors / series), not the whole table.
    seen = {row._mapping[id_column_name] for row in rows}
    emptied = {i for i in ids if i is not None and i not in seen}
    if emptied:
        session.execute(
            update(model_class)
            .where(pk_col.in_(emptied))
            .values(_zeroed_summary())
        )


def _zeroed_summary() -> dict:
    """Aggregate columns reset to their "no novels" values.

    Only the four counters.  A *derived name* column (``author_name`` /
    ``series_name``) is deliberately left alone: "this row has no novels left"
    says nothing about the name the user still sees on the author / series page.
    """
    return {
        C.COL_NOVEL_COUNT: 0,
        C.COL_VIEWS: 0,
        C.COL_LIKES: 0,
        C.COL_TEXTS: 0,
    }


def _upsert_summary_row(
    session: Session,
    model_class: type[ModelType],
    pk_col,
    target_id: int,
    values: dict,
) -> None:
    """``INSERT … ON CONFLICT DO UPDATE`` one aggregate row."""
    insert_values = {pk_col.name: target_id, **values}
    session.execute(
        postgresql_insert(model_class)
        # ``index_elements`` must be a keyword: as a positional arg it binds to
        # ``constraint`` and crashes.
        .values(insert_values)
        .on_conflict_do_update(index_elements=[pk_col], set_=values)
    )


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
