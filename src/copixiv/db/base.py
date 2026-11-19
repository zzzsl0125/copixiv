"""Base repository — shared SQLAlchemy helpers."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from copixiv.db import constants as C
from copixiv.db import models


def model_to_dict(obj: Any) -> dict:
    """Convert a single ORM instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def models_to_dicts(objs: list[Any]) -> list[dict]:
    """Convert a list of ORM instances to plain dicts."""
    return [model_to_dict(o) for o in objs]


def update_summary[ModelType: Any](
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
    target_ids = {i for i in ids if i is not None}
    if not target_ids:
        return

    extra = list(extra_columns or ())
    columns_to_select = [
        source_col.label(id_column_name),
        func.count(models.Novel.id).label(C.COL_NOVEL_COUNT),
        func.coalesce(func.sum(models.Novel.view), 0).label(C.COL_VIEWS),
        func.coalesce(func.sum(models.Novel.like), 0).label(C.COL_LIKES),
        func.coalesce(func.sum(models.Novel.text), 0).label(C.COL_TEXTS),
        *extra,
    ]
    agg_select = select(*columns_to_select).where(source_col.in_(target_ids)).group_by(source_col)

    insert_stmt = postgresql_insert(model_class).from_select(
        [col.key for col in agg_select.selected_columns if col.key is not None],
        agg_select,
    )
    set_dict: dict[str, Any] = {
        C.COL_NOVEL_COUNT: insert_stmt.excluded[C.COL_NOVEL_COUNT],
        C.COL_VIEWS: insert_stmt.excluded[C.COL_VIEWS],
        C.COL_LIKES: insert_stmt.excluded[C.COL_LIKES],
        C.COL_TEXTS: insert_stmt.excluded[C.COL_TEXTS],
    }
    for col in extra:
        # Preserve stored display names / author links when max(...) is NULL.
        set_dict[col.key] = func.coalesce(
            insert_stmt.excluded[col.key],
            getattr(model_class, col.key),
        )

    session.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=[pk_col],
            set_=set_dict,
        )
    )

    has_novel = select(1).where(source_col == pk_col).exists()
    session.execute(
        update(model_class)
        .where(pk_col.in_(target_ids), ~has_novel)
        .values(
            {
                C.COL_NOVEL_COUNT: 0,
                C.COL_VIEWS: 0,
                C.COL_LIKES: 0,
                C.COL_TEXTS: 0,
            }
        )
    )


def ensure_parent_rows(
    session: Session,
    *,
    author_ids: Iterable[int | None] = (),
    series_ids: Iterable[int | None] = (),
) -> None:
    """INSERT ... ON CONFLICT DO NOTHING placeholder rows for FK safety."""
    a_ids = sorted({aid for aid in author_ids if aid})
    s_ids = sorted({sid for sid in series_ids if sid})
    flushed = False
    if a_ids:
        session.execute(
            postgresql_insert(models.Author)
            .values([{"author_id": aid} for aid in a_ids])
            .on_conflict_do_nothing()
        )
        flushed = True
    if s_ids:
        session.execute(
            postgresql_insert(models.Series)
            .values([{"series_id": sid} for sid in s_ids])
            .on_conflict_do_nothing()
        )
        flushed = True
    if flushed:
        session.flush()


def refresh_parent_summaries(
    session: Session,
    *,
    author_ids: Iterable[int | None] = (),
    series_ids: Iterable[int | None] = (),
) -> None:
    """Recompute author and series summary counters/names for affected IDs."""
    a_ids = {aid for aid in author_ids if aid is not None}
    s_ids = {sid for sid in series_ids if sid is not None}
    if a_ids:
        update_summary(
            session,
            models.Author,
            C.COL_AUTHOR_ID,
            a_ids,
            extra_columns=[
                func.max(models.Novel.author_name).label(C.COL_AUTHOR_NAME),
            ],
        )
    if s_ids:
        update_summary(
            session,
            models.Series,
            C.COL_SERIES_ID,
            s_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
                func.max(models.Novel.author_id).label(C.COL_AUTHOR_ID),
            ],
        )


def finalize_parent_links(
    session: Session,
    *,
    author_ids: Iterable[int | None] = (),
    series_ids: Iterable[int | None] = (),
) -> None:
    """Finalize author/series aggregates after novel link changes."""
    refresh_parent_summaries(
        session,
        author_ids=author_ids,
        series_ids=series_ids,
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
