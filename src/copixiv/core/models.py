"""Pure domain entities — Pydantic models with no ORM coupling.

Merged from ``domain/models/*`` (8 files + ``__init__.py`` re-exports).
"""

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Novel domain entities — pure Pydantic models.
# ---------------------------------------------------------------------------


class EpubStatus(IntEnum):
    """EPUB conversion status for a novel."""
    NO = 0
    PENDING = 1
    DONE = 2


class Novel(BaseModel):
    """A Pixiv novel, stored locally after download.

    Canonical domain object for the write path: the factory functions in
    ``core/services.py`` return instances of this model and repositories
    accept them (translating to ORM rows internally).  Plain dicts only
    appear at the HTTP wire boundary.

    Types mirror the database columns — ``create_time`` is a *string*
    (ISO date/time from the Pixiv API, stored in a String column), not a
    ``datetime`` object.
    """

    id: int
    title: str
    author_id: int
    author_name: str | None = None
    path: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0  # 正文字符数（content 才是正文文本）
    caption: str | None = None
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    create_time: str | None = None
    # Shuffle key for random browsing — persisted column.  The write path
    # assigns a random value on insert (never overwritten on refresh);
    # the read path uses it for keyset pagination in random order.
    shuffle: int = 0
    # ``has_epub=None`` means "don't overwrite the stored value" on a
    # metadata-only refresh; the DB column itself is never NULL in practice.
    has_epub: EpubStatus | None = None

    # Transient — not persisted, popped before the DB upsert
    tags: list[str] = Field(default_factory=list)
    content: str | None = None
    images: dict | None = None
    illusts: dict | None = None
    cover_url: str | None = None

    # Display flags — not persisted, joined from other tables
    is_favourite: bool = False
    is_special_follow: bool = False


# ---------------------------------------------------------------------------
# Author domain entity.
# ---------------------------------------------------------------------------


class Author(BaseModel):
    """A Pixiv author, with aggregated stats from their novels."""

    author_id: int
    author_name: str | None = None
    novel_count: int = 0
    like: int = 0
    view: int = 0
    text: int = 0
    last_update: datetime | None = None


# ---------------------------------------------------------------------------
# Series domain entity.
# ---------------------------------------------------------------------------


class Series(BaseModel):
    """A Pixiv series, with aggregated stats from its novels."""

    series_id: int
    series_name: str | None = None
    novel_count: int = 0
    author_id: int | None = None
    like: int = 0
    view: int = 0
    text: int = 0


# ---------------------------------------------------------------------------
# Tag domain entities.
# ---------------------------------------------------------------------------


class TagPreferenceType(StrEnum):
    favourite = "favourite"
    blocked = "blocked"


class Tag(BaseModel):
    """A tag, with reference count across all novels."""

    id: int = 0
    name: str
    reference_count: int = 0


class TagPreference(BaseModel):
    """User preference for a tag (favourite or blocked)."""

    id: int = 0
    tag: str
    preference: TagPreferenceType
    sort_index: int = 0


class TagAlias(BaseModel):
    """Alias mapping: `source` tag is replaced by `target` tag."""

    id: int = 0
    source: str
    target: str


# ---------------------------------------------------------------------------
# Task domain entities.
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskHistory(BaseModel):
    """Record of a completed or in-progress task execution."""

    id: int = 0
    name: str
    # Registered function name — the dedup key (separate from ``name``,
    # which is the display name).  See ``task_history.task_func``.
    task_func: str | None = None
    arguments: dict | None = None
    status: str = TaskStatus.PENDING
    start_time: datetime
    end_time: datetime | None = None
    duration: float | None = None
    result: dict | None = None
    # Live progress (S2 d) — column only here; wire-up lands later.
    progress: str | None = None


class ScheduledTask(BaseModel):
    """A task scheduled to run on a cron expression."""

    id: int = 0
    name: str
    task: str
    cron: str
    params: dict | None = None
    is_enabled: bool = False
    config: dict | None = None
    sort_index: int = 0


# ---------------------------------------------------------------------------
# TaskResult — structured return value for all background tasks.
#
# Replaces the ad-hoc ``list[str] | int | None`` returns so that:
# * Novel-discovery tasks can clearly mark which titles are new.
# * Maintenance tasks can report a plain summary without polluting the
#   ``new_novel_titles`` column.
# * The notifier can decide *how* to format the message based on whether
#   the task actually discovered novels.
# ---------------------------------------------------------------------------


class TaskResult(BaseModel):
    """Structured result from a task execution.

    Attributes:
        summary: Human-readable one-line summary used in notifications and
            log output.  Always populated.
        new_novel_titles: Titles of novels that were **newly discovered and
            persisted** during this task run.  Only populated by tasks that
            actually fetch/download novels (e.g. ``novel_follow``,
            ``author_fetch``).  Maintenance tasks leave this empty.
        new_novel_count: Total count of newly persisted novels.  Always
            mirrors ``len(new_novel_titles)`` (enforced by a validator), so
            callers never need to set it explicitly.
    """

    summary: str = ""
    new_novel_titles: list[str] = Field(default_factory=list)
    new_novel_count: int = 0

    @model_validator(mode="after")
    def _sync_count(self) -> "TaskResult":
        if self.new_novel_titles:
            self.new_novel_count = len(self.new_novel_titles)
        else:
            self.new_novel_count = 0
        return self


# ---------------------------------------------------------------------------
# Token domain entity — Pixiv refresh token.
# ---------------------------------------------------------------------------


class Token(BaseModel):
    """A Pixiv refresh token stored for account authentication."""

    id: int = 0
    name: str
    token: str
    premium: bool = False
    valid: bool = True
    sort_index: int = 0


# ---------------------------------------------------------------------------
# Search history domain entity.
# ---------------------------------------------------------------------------


class SearchHistory(BaseModel):
    """A previously executed search, stored for quick recall."""

    id: int = 0
    type: str
    value: str
    display_value: str | None = None
    timestamp: datetime


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
