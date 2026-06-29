"""Author domain entity."""

from pydantic import BaseModel


class Author(BaseModel):
    """A Pixiv author, with aggregated stats from their novels."""

    author_id: int
    author_name: str | None = None
    novel_count: int = 0
    like: int = 0
    view: int = 0
    text: int = 0
    last_update: str | None = None
