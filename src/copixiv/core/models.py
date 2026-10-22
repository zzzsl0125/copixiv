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
    """EPUB conversion status for a novel.

    ``NO`` and ``NO_IMAGES`` are different facts, and conflating them cost
    this project five weeks of invisible breakage (2026-09 追溯):

    * ``NO`` (0) — **unclassified**.  A legacy value, and the column default.
      It carries no claim: the row may or may not need an EPUB, and only the
      text file can settle it.  The reconciler re-examines these rows so they
      eventually reach a terminal state.
    * ``NO_IMAGES`` (3) — **terminal: nothing to build**.  Means *no EPUB will
      ever be produced for this row*, for either of two reasons: the body has
      no image placeholder to embed, **or** the placeholders' images are gone
      for good (author deleted them / the URLs are dead, confirmed by the
      stale-image rule).  The reconciler never re-reads the text of a row in
      this state.
    * ``MISSING_SOURCE`` (4) — **terminal: no local text**.  ``novel.path`` is
      set but the text file is not on disk (and no renamed copy exists).  The
      row can never be *built from* anything local, so it must not sit in
      ``NO``/``PENDING`` pretending work is pending.  It is recoverable only
      by a re-download, which the UI must offer explicitly.

    Historically ``0`` meant "no images" *and* "don't look at me" at once
    (the reconciler filtered on ``has_epub > 0``), so rows demoted by the
    stale-image rule — or clobbered to 0 by every metadata refresh — could
    never be repaired.  Splitting the two meanings is the point; ``4`` exists
    for the same reason (one more fact that must not hide behind ``0``).
    """
    NO = 0
    PENDING = 1
    DONE = 2
    NO_IMAGES = 3
    MISSING_SOURCE = 4


class Novel(BaseModel):
    """A Pixiv novel, stored locally after download.

    Read-path domain object: the read repository builds these from ORM
    rows.  The *write* path is carried by
    :class:`~copixiv.core.draft.NovelDraft` (built by the
    factory functions in ``core/draft.py``); plain dicts only
    appear at the HTTP wire boundary.

    Types mirror the database columns: ``create_time`` is a ``timestamptz``
    in PostgreSQL, so the read model carries a ``datetime`` (the *write*
    carrier, :class:`~copixiv.core.draft.NovelDraft`, keeps the source's
    string and converts it at persist time).

    The transient asset fields (``content`` / ``images`` / ``illusts`` /
    ``cover_url``) intentionally live **only** on ``NovelDraft``: their real
    readers (``storage/image_downloader.py``,
    ``storage/epub/builder.py``) take the write carrier, so a copy on the
    read model would have no consumer and would mislead.
    """

    id: int
    title: str
    # ``author_id`` is nullable: novels orphaned by the SQLite→PG migration
    # (author row missing) keep NULL, and a deleted Pixiv author stays NULL.
    # Mirrors ``Series.author_id``.
    author_id: int | None = None
    author_name: str | None = None
    path: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0  # 正文字符数（content 才是正文文本）
    caption: str | None = None
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    create_time: datetime | None = None
    # Shuffle key for random browsing — persisted column.  The write path
    # assigns a random value on insert (never overwritten on refresh);
    # the read path uses it for keyset pagination in random order.
    shuffle: int = 0
    # ``has_epub=None`` means "don't overwrite the stored value" on a
    # metadata-only refresh; the DB column itself is never NULL in practice.
    has_epub: EpubStatus | None = None
    # Where the row came from — ``'pixiv'`` (scraper) or ``'local'`` (a TXT /
    # EPUB uploaded through 「我的上传」, docs/LIBRARY_DESIGN.md §2).  The list
    # response carries it so the card can show a 「本地」 badge.
    source: str = "pixiv"
    # Local books only: sha256 of the normalised body (the dedup key);
    # NULL for Pixiv rows.
    source_key: str | None = None

    # Tags are the ``novel.tags text[]`` column (read straight from the row).
    tags: list[str] = Field(default_factory=list)

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
    # Registered function name (separate from ``name``, which is the display
    # name).  No dedup semantics — see ``task_history.task_func``.
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
            log output.  Always populated.  **Never parse this** — it is UI
            copy, and a wording change would silently break the caller.
        new_novel_titles: Titles of novels that were **newly discovered and
            persisted** during this task run.  Only populated by tasks that
            actually fetch/download novels (e.g. ``novel_follow``,
            ``author_fetch``).  Maintenance tasks leave this empty.
        new_novel_count: Total count of newly persisted novels.  Always
            mirrors ``len(new_novel_titles)`` (enforced by a validator), so
            callers never need to set it explicitly.
        failed_ids: IDs this task attempted and could **not** complete — the
            machine-readable counterpart of "this one failed".  Callers that
            need per-item outcomes must read this instead of pattern-matching
            ``summary`` (``failed_retry`` tested ``"获取失败" not in summary``,
            which broke on any wording change).
    """

    summary: str = ""
    new_novel_titles: list[str] = Field(default_factory=list)
    new_novel_count: int = 0
    failed_ids: list[int] = Field(default_factory=list)

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
