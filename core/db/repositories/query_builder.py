# -*- coding: utf-8 -*-
from typing import Type
from sqlalchemy import Table, MetaData, select, text, and_, or_, desc, asc, func
from .base_repository import ModelType
from .fts_manager import FTSManager
from .. import constants as C

class BaseQueryBuilder:
    """
    A generic query builder for constructing complex SQLAlchemy ORM select statements.
    """
    # Class-level cache for reflected FTS tables to avoid repeated reflection
    _fts_table_cache = {}
    
    def __init__(self, session, main_model: Type[ModelType]):
        self.session = session
        self.main_model = main_model
        # Explicitly select all columns from the model's table.
        # This ensures the result rows can be directly converted to dicts.
        self.stmt = select(*self.main_model.__table__.c)
        self.params = {}
        self.wheres = []
        self.metadata = MetaData() # For loading FTS tables

    def with_keyword(self, keyword: str | None, fts_table_name: str, apply_to=None):
        stmt = apply_to if apply_to is not None else self.stmt
        condition = None

        if keyword:
            # Use cached FTS table reflection to avoid repeated reflection
            cache_key = f"{fts_table_name}_{id(self.session.get_bind())}"
            
            if cache_key not in self._fts_table_cache:
                # Reflect the FTS table and cache it
                fts_table = Table(fts_table_name, self.metadata, autoload_with=self.session.get_bind())
                self._fts_table_cache[cache_key] = fts_table
            else:
                fts_table = self._fts_table_cache[cache_key]
            
            join_condition = text(f"{self.main_model.__tablename__}.id = {fts_table.name}.rowid")
            stmt = stmt.join(fts_table, join_condition)
            
            fts_query = FTSManager.tokenize(keyword)
            
            # Use MATCH operator
            condition = text(f"{fts_table_name} MATCH :fts_query")
            self.params['fts_query'] = fts_query
            
        if apply_to is not None:
            return stmt, condition
        else:
            if condition is not None:
                self.wheres.append(condition)
            self.stmt = stmt
            return self

    def with_simple_keyword(self, keyword: str | None, field_name: str):
        """Simple keyword search using LIKE operator"""
        if keyword:
            field = getattr(self.main_model, field_name)
            self.wheres.append(field == keyword)
        return self

    def with_pagination(self, last_item: dict | None, order_by: str, order_direction: str, apply_to=None):
        stmt = apply_to if apply_to is not None else self.stmt
        
        if last_item and order_by != 'random':
            last_id = last_item.get('id') # Use 'id' directly for tie-breaking
            last_val = last_item.get(order_by)

            if last_id is None:
                return stmt

            last_val = self._convert_value_by_field_type(last_val, order_by)

            order_col = getattr(self.main_model, order_by)
            id_col = getattr(self.main_model, 'id') # Use 'id' directly for tie-breaking

            pagination_condition = None
            
            tie_break = and_(order_col == last_val, id_col < last_id)

            if order_direction == 'DESC':
                if last_val is not None:
                    pagination_condition = or_(order_col < last_val, tie_break)
                else:  # last_val is NULL, so we are at the end of non-null values
                    pagination_condition = and_(order_col.is_(None), id_col < last_id)
            else:  # ASC
                if last_val is not None:
                    pagination_condition = or_(order_col > last_val, tie_break)
                else:  # last_val is NULL
                    pagination_condition = and_(order_col.is_(None), id_col < last_id)
            
            stmt = stmt.where(pagination_condition)

        if apply_to is not None:
            return stmt
        else:
            if pagination_condition is not None:
                self.wheres.append(pagination_condition)
            return self

    def _convert_value_by_field_type(self, value, field_name: str):
        if value is None:
            return value

        field = getattr(self.main_model, field_name, None)
        if field is None:
            return value

        try:
            column_type = field.property.columns[0].type
            python_type = column_type.python_type
            
            if python_type is int:
                return int(value)
        except (ValueError, TypeError, AttributeError):
            pass
            
        return value

    def _apply_ordering(self, stmt, order_by: str, order_direction: str):
        """应用排序逻辑到给定的语句。返回排序后的语句。"""
        if order_by == 'random':
            return stmt.order_by(func.random())
        elif order_by != 'none':
            order_col = getattr(self.main_model, order_by)
            id_col = getattr(self.main_model, 'id') # Use 'id' directly for tie-breaking
            
            if order_direction == 'DESC':
                return stmt.order_by(order_col.desc().nullslast(), desc(id_col))
            else:
                return stmt.order_by(order_col.asc().nullslast(), desc(id_col))
        return stmt
    
    def with_ordering(self, order_by: str, order_direction: str, apply_to=None):
        stmt = apply_to if apply_to is not None else self.stmt
        stmt = self._apply_ordering(stmt, order_by, order_direction)
        
        if apply_to is not None:
            return stmt
        else:
            self.stmt = stmt
            return self

    def with_limit(self, per_page: int | None, apply_to=None):
        stmt = apply_to if apply_to is not None else self.stmt
        if per_page is not None and per_page > 0:
            stmt = stmt.limit(per_page)

        if apply_to is not None:
            return stmt
        else:
            self.stmt = stmt
            return self

    def build(self):
        if self.wheres:
            self.stmt = self.stmt.where(and_(*self.wheres))
        return self.stmt, self.params
