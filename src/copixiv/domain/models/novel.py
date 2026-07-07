"""Novel domain entities — pure Pydantic models."""

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, Field


class EpubStatus(IntEnum):
    """EPUB conversion status for a novel."""
    NO = 0
    PENDING = 1
    DONE = 2


class Novel(BaseModel):
    """A Pixiv novel, stored locally after download."""

    id: int
    title: str
    author_id: int
    author_name: str | None = None
    path: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    caption: str | None = None
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    create_time: datetime | None = None
    has_epub: EpubStatus = EpubStatus.NO

    # Transient — not persisted directly, joined from other tables
    tags: list[str] = Field(default_factory=list)
    is_favourite: bool = False
    is_special_follow: bool = False
