"""Search history domain entity."""

from datetime import datetime

from pydantic import BaseModel


class SearchHistory(BaseModel):
    """A previously executed search, stored for quick recall."""

    id: int = 0
    type: str
    value: str
    display_value: str | None = None
    timestamp: datetime
