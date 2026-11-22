"""Pure domain entities, shared ID rules, and value objects — zero framework coupling."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field

from copixiv.core.exceptions import ValidationError


class NovelSource(StrEnum):
    """Origin of a novel row in ``novel.source``."""

    PIXIV = "pixiv"
    LOCAL = "local"


SOURCE_PIXIV = NovelSource.PIXIV.value
SOURCE_LOCAL = NovelSource.LOCAL.value
UNKNOWN_AUTHOR = "未知作者"


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


class TaskResult(BaseModel):
    """Structured result returned by all background tasks."""

    summary: str = ""
    new_novel_titles: list[str] = Field(default_factory=list)
    failed_ids: list[int] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def new_novel_count(self) -> int:
        """Count of newly persisted novels derived from ``new_novel_titles``."""
        return len(self.new_novel_titles)


def positive_pixiv_id(raw: Any, label: str, hint: str) -> int:
    """Validate a positive 64-bit integer Pixiv ID (author or series)."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(f"{label} ID 必须是整数")
    if raw <= 0:
        raise ValidationError(f"{label} ID 必须是正整数（{hint}）")
    if raw > 2**63 - 1:
        raise ValidationError(f"{label} ID 超出取值范围")
    return raw


def author_id(raw: Any) -> int:
    """Validate a positive Pixiv author ID."""
    return positive_pixiv_id(raw, "作者", "pixiv 用户 ID")


def series_id(raw: Any) -> int:
    """Validate a positive Pixiv series ID."""
    return positive_pixiv_id(raw, "系列", "pixiv 系列 ID")


__all__ = [
    "EpubStatus",
    "NovelSource",
    "SOURCE_LOCAL",
    "SOURCE_PIXIV",
    "TaskResult",
    "UNKNOWN_AUTHOR",
    "author_id",
    "positive_pixiv_id",
    "series_id",
]
