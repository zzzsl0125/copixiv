"""Pure domain entities — Pydantic models with no ORM coupling."""

from .novel import Novel, EpubStatus
from .author import Author
from .series import Series
from .tag import Tag, TagPreference, TagAlias, TagPreferenceType
from .task import TaskHistory, ScheduledTask, TaskStatus
from .task_result import TaskResult
from .token import Token
from .search import SearchHistory

__all__ = [
    "Novel",
    "EpubStatus",
    "Author",
    "Series",
    "Tag",
    "TagPreference",
    "TagAlias",
    "TagPreferenceType",
    "TaskHistory",
    "ScheduledTask",
    "TaskStatus",
    "TaskResult",
    "Token",
    "SearchHistory",
]
