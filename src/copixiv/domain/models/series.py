"""Series domain entity."""

from pydantic import BaseModel


class Series(BaseModel):
    """A Pixiv series, with aggregated stats from its novels."""

    series_id: int
    series_name: str | None = None
    novel_count: int = 0
    author_id: int | None = None
    like: int = 0
    view: int = 0
    text: int = 0
