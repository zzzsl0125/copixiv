# -*- coding: utf-8 -*-
from typing import Literal, Any, Union, Type, TypeVar, List, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text, select, func, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Result
from sqlalchemy.sql import Executable

import core.db.constants as C

ModelType = TypeVar('ModelType', bound=Any)

class BaseRepository:
    """
    Base class for all repositories, providing common database execution logic.
    """
    def __init__(self, session: Session):
        """
        Initializes the repository with an SQLAlchemy Session.
        """
        self.session = session

    def _execute(
        self,
        sql: Union[str, Executable],
        params: Union[Dict[str, Any], tuple, List[Any]] = (),
        fetch: Literal['none', 'one', 'all'] = 'none'
    ) -> Union[None, Dict[str, Any], List[Dict[str, Any]]]:
        """
        Executes a raw SQL statement using the repository's session.
        """
        executable = sql if isinstance(sql, Executable) else text(sql)
        
        result: Result = self.session.execute(executable, params)
        if fetch == 'one':
            row = result.fetchone()
            return dict(row._mapping) if row else None
        if fetch == 'all':
            return [dict(row._mapping) for row in result.fetchall()]
        return None

    def get_by_id(self, model_class: Type[ModelType], item_id: Any) -> Optional[ModelType]:
        """Generic get by ID using ORM."""
        return self.session.get(model_class, item_id)

    def get_all(self, model_class: Type[ModelType], limit: int = 100, offset: int = 0) -> List[ModelType]:
        """Generic get all."""
        stmt = select(model_class).limit(limit).offset(offset)
        return self.session.execute(stmt).scalars().all()

    def count(self, model_class: Type[ModelType]) -> int:
        """Generic count."""
        stmt = select(func.count()).select_from(model_class)
        return self.session.execute(stmt).scalar()

    def get_summary_item(self, model_class: Type[ModelType], item_id: int) -> dict | None:
        """Generic function to get an item from a summary table using its ORM model."""
        # Assuming single primary key
        pk_column = list(model_class.__mapper__.primary_key)[0]
        stmt = select(model_class).where(pk_column == item_id)
        
        result = self.session.execute(stmt).scalar_one_or_none()
        if result:
            # Convert ORM object to dict to maintain consistency
            return {c.name: getattr(result, c.name) for c in result.__table__.columns}
        return None

    def _update_summary(self, model_class: Type[ModelType], id_column_name: str, ids: list[int] | int | None = None, extra_columns: list = None):
        """
        A generic method to update summary statistics for a given model (e.g., Author, Series).
        If ids is None, updates all records.
        If ids is an int, updates that single record.
        If ids is a list, updates those records.
        :param extra_columns: Optional list of additional columns to select from Novel table (e.g. for aggregation)
        """
        # Local import to avoid circular dependency
        from .. import models

        if ids is not None and isinstance(ids, list) and not ids:
             return

        pk_col = getattr(model_class, id_column_name)

        # Subquery to calculate new summary data from the Novel table
        # We use models.Novel
        columns_to_select = [
                getattr(models.Novel, id_column_name),
                func.count(models.Novel.id).label(C.COL_NOVEL_COUNT),
                func.sum(models.Novel.view).label(C.COL_VIEWS),
                func.sum(models.Novel.like).label(C.COL_LIKES),
                func.sum(models.Novel.text).label(C.COL_TEXTS)
            ]
        
        if extra_columns:
            columns_to_select.extend(extra_columns)

        select_stmt = select(*columns_to_select)

        if ids is not None:
            if isinstance(ids, int):
                ids = [ids]
            select_stmt = select_stmt.where(getattr(models.Novel, id_column_name).in_(ids))
            
        select_stmt = select_stmt.group_by(getattr(models.Novel, id_column_name))
        
        # Execute the select statement first to get the summary data
        results = self.session.execute(select_stmt).all()

        # Update the records individually
        for row in results:
            summary_data = row._mapping
            target_id = summary_data[id_column_name]
            
            if target_id is None:
                continue

            # Prepare values for update/insert
            values = {
                C.COL_NOVEL_COUNT: summary_data[C.COL_NOVEL_COUNT],
                C.COL_VIEWS: summary_data[C.COL_VIEWS],
                C.COL_LIKES: summary_data[C.COL_LIKES],
                C.COL_TEXTS: summary_data[C.COL_TEXTS]
            }

            # Add extra columns to values if present in result
            for key, val in summary_data.items():
                if key != id_column_name and key not in values and val is not None:
                     values[key] = val

            # Prepare insert dictionary (combine PK and values)
            insert_values = {pk_col.name: target_id, **values}

            # Use SQLite UPSERT (ON CONFLICT DO UPDATE)
            stmt = sqlite_insert(model_class).values(insert_values)
            
            do_update_stmt = stmt.on_conflict_do_update([pk_col], set_=values)
            
            self.session.execute(do_update_stmt)

