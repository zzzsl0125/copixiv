"""Novel domain entities — pure Pydantic models."""

from datetime import datetime
from pydantic import BaseModel, Field


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
    create_time: str | None = None
    has_epub: int = 0  # 0=no, 1=pending, 2=done

    # Transient — not persisted directly, joined from other tables
    tags: list[str] = Field(default_factory=list)
    is_favourite: int = 0
    is_special_follow: int = 0


class NovelTag(BaseModel):
    """Join table: novel <-> tag."""

    novel_id: int
    tag_id: int


class Favourite(BaseModel):
    """A favourited novel."""

    novel_id: int


class SpecialFollow(BaseModel):
    """A specially followed author."""

    author_id: int
