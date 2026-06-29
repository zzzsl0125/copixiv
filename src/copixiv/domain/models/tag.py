"""Tag domain entities."""

from enum import StrEnum
from pydantic import BaseModel


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
