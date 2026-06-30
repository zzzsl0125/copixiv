"""Pure domain entities — Pydantic models with no ORM coupling."""

from .novel import Novel, NovelTag, Favourite, SpecialFollow
from .author import Author
from .series import Series
from .tag import Tag, TagPreference, TagAlias, TagPreferenceType
from .task import TaskHistory, ScheduledTask, TaskStatus
from .token import Token
from .search import SearchHistory
from .misc import FailedNovel, ProcessedPeriod, NovelEpubConversion

__all__ = [
    "Novel",
    "NovelTag",
    "Favourite",
    "Author",
    "Series",
    "Tag",
    "TagPreference",
    "TagAlias",
    "TagPreferenceType",
    "TaskHistory",
    "ScheduledTask",
    "TaskStatus",
    "Token",
    "SearchHistory",
    "FailedNovel",
    "ProcessedPeriod",
    "NovelEpubConversion",
    "SpecialFollow",
]
