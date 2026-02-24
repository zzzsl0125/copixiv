# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Literal
from sqlalchemy import select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .base_repository import BaseRepository
from .. import models
from .. import constants as C

VALID_DATE_TARGETS = {'day', 'month', 'year'}

class System(BaseRepository):
    """
    Repository for system-level database operations like managing failures,
    search history, and processed date markers.
    """
    def record_failure(self, novel, failure_type: str, error_message: str):
        """Records a failure for a novel download or processing."""
        # Assuming novel is an object with id and title attributes
        novel_id = getattr(novel, 'id', None)
        novel_title = getattr(novel, 'title', None)
        
        timestamp = datetime.now().isoformat()
        
        stmt = sqlite_insert(models.FailedNovel).values(
            novel_id=novel_id,
            novel_title=novel_title,
            failure_type=failure_type,
            error_message=str(error_message),
            failed_at=timestamp
        ).on_conflict_do_nothing(
            index_elements=['novel_id', 'failure_type']
        )
        self.session.execute(stmt)

    def mark_date(self, date_str: str, target: Literal['day', 'month', 'year'] = 'day'):
        """Marks a date, month, or year as processed."""
        if target not in VALID_DATE_TARGETS:
            raise ValueError(f"Invalid target for mark_date: {target}")
        self._validate_date_string(date_str, target)
        
        stmt = sqlite_insert(models.ProcessedPeriod).values(
            period_type=target,
            period_value=date_str
        ).on_conflict_do_nothing(
            index_elements=['period_type', 'period_value']
        )
        self.session.execute(stmt)

    def check_date(self, date_str: str, target: Literal['day', 'month', 'year'] = 'day') -> bool:
        """Checks if a date, month, or year has been marked as processed."""
        if target not in VALID_DATE_TARGETS:
            raise ValueError(f"Invalid target for check_date: {target}")
        self._validate_date_string(date_str, target)
        
        stmt = select(models.ProcessedPeriod).where(
            models.ProcessedPeriod.period_type == target,
            models.ProcessedPeriod.period_value == date_str
        ).limit(1)
        result = self.session.execute(stmt).scalar_one_or_none()
        return result is not None

    def clear_time_table(
        self,
        target: Literal['day', 'month', 'year'],
        date_str: str | None = None
    ):
        """
        Clears entries from the processed_periods table for a specified period type.
        """
        if target not in VALID_DATE_TARGETS:
            raise ValueError(f"Invalid target for clear_time_table: {target}")

        stmt = delete(models.ProcessedPeriod).where(models.ProcessedPeriod.period_type == target)
        if date_str:
            self._validate_date_string(date_str, target)
            stmt = stmt.where(models.ProcessedPeriod.period_value == date_str)
            
        self.session.execute(stmt)

    def add_search_history(self, queries: str, sort_by: str, sort_order: str) -> int:
        """Adds or updates a search history entry and returns its ID."""
        timestamp = datetime.now().isoformat()
        
        # Using sqlite_insert for upsert behavior
        stmt = sqlite_insert(models.SearchHistory).values(
            queries=queries,
            sort_by=sort_by,
            sort_order=sort_order,
            timestamp=timestamp
        ).on_conflict_do_update(
            index_elements=['queries'],
            set_={
                'sort_by': sort_by,
                'sort_order': sort_order,
                'timestamp': timestamp
            }
        ).returning(models.SearchHistory.id) # Only works for recent SQLite/SQLAlchemy versions?
        
        # Fallback if returning() is not supported or simpler logic
        # Actually returning works with SQLAlchemy for SQLite if supported by driver
        # But let's check if we can rely on it. Python 3.10+ sqlite3 supports RETURNING.
        # If not, we have to select.
        
        try:
            result = self.session.execute(stmt).scalar()
            if result:
                return result
        except Exception:
            # Fallback
            pass
            
        # Select ID if upsert returned nothing (e.g. no change? No, upsert always writes or updates)
        stmt = select(models.SearchHistory.id).where(models.SearchHistory.queries == queries)
        return self.session.execute(stmt).scalar()

    def update_search_history_display_text(self, history_id: int, display_text: str):
        """Updates the display_text for a given search history entry."""
        stmt = (
            models.SearchHistory.__table__.update()
            .where(models.SearchHistory.id == history_id)
            .values(display_text=display_text)
        )
        self.session.execute(stmt)

    def get_search_history(self, limit: int = 30) -> list[dict]:
        """Retrieves search history."""
        stmt = select(models.SearchHistory).order_by(models.SearchHistory.timestamp.desc()).limit(limit)
        result = self.session.execute(stmt).scalars().all()
        return [{c.name: getattr(n, c.name) for c in n.__table__.columns} for n in result]

    def get_failed_novels(self) -> list[dict]:
        """Retrieves all entries from the failed_novel table."""
        stmt = select(models.FailedNovel).order_by(models.FailedNovel.failed_at.asc())
        result = self.session.execute(stmt).scalars().all()
        return [{c.name: getattr(n, c.name) for c in n.__table__.columns} for n in result]

    def delete_failed_novel(self, failed_novel_id: int):
        """Deletes a specific entry from the failed_novel table by its primary key."""
        stmt = delete(models.FailedNovel).where(models.FailedNovel.id == failed_novel_id)
        self.session.execute(stmt)

    def _validate_date_string(self, date_str: str, target: Literal['day', 'month', 'year']):
        """Validates a date string against the expected format for the target table."""
        formats = {
            'day': '%Y-%m-%d',
            'month': '%Y-%m',
            'year': '%Y'
        }
        try:
            datetime.strptime(date_str, formats[target])
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid date string '{date_str}' for target '{target}'. Expected format: {formats.get(target)}") from e

    def vacuum_database(self):
        """Vacuums the database to reclaim space."""
        # Execute VACUUM in autocommit mode
        with self.session.bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            from sqlalchemy import text
            connection.execute(text("VACUUM"))
