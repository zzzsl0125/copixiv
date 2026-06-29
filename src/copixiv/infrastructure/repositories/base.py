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
        return self.session.execute(stmt).scalar() or 0

    def get_summary_item(
        self, model_class: type[ModelType], item_id: int
    ) -> dict | None:
        pk_col = list(model_class.__mapper__.primary_key)[0]
        stmt = select(model_class).where(pk_col == item_id)
        result = self.session.execute(stmt).scalar_one_or_none()
        return model_to_dict(result) if result else None

    def _update_summary(
        self,
        model_class: type[ModelType],
        id_column_name: str,
        ids: set[int] | int | None = None,
        extra_columns: list | None = None,
    ) -> None:
        from ..database import models

        if ids is None:
            # caller intends to update ALL — but this method needs a concrete ID set
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

        results = self.session.execute(select_stmt).all()

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
            self.session.execute(stmt)
