"""Novel domain entities — pure Pydantic models."""

from enum import IntEnum

from pydantic import BaseModel, Field


class EpubStatus(IntEnum):
    """EPUB conversion status for a novel."""
    NO = 0
    PENDING = 1
    DONE = 2


class Novel(BaseModel):
    """A Pixiv novel, stored locally after download.

    Canonical domain object for the write path: the factory functions in
    ``domain/services/novel_factory.py`` return instances of this model and
    repositories accept them (translating to ORM rows internally).  Plain
    dicts only appear at the HTTP wire boundary.

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
