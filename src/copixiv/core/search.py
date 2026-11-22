"""Pure search specification, keyword parsing, ordering enums, and blocklist policy."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class OrderBy(StrEnum):
    """Supported novel ordering axes."""

    ID = "id"
    LIKE = "like"
    VIEW = "view"
    TEXT = "text"
    CREATE_TIME = "create_time"
    RANDOM = "random"


class SortableOrderBy(StrEnum):
    """Deterministic novel ordering axes for explicit ID lists."""

    ID = "id"
    LIKE = "like"
    VIEW = "view"
    TEXT = "text"
    CREATE_TIME = "create_time"


class OrderDirection(StrEnum):
    """Sort direction (case-insensitive normalization)."""

    ASC = "ASC"
    DESC = "DESC"

    @classmethod
    def _missing_(cls, value: object) -> OrderDirection | None:
        if isinstance(value, str):
            upper = value.strip().upper()
            for member in cls:
                if member.value == upper:
                    return member
        return None


SearchConditions = list[tuple[str, str]]

_BARE_ID_RE = re.compile(r"^\d{7,}$")

VALID_DSL_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "author_id",
        "series_id",
        "like",
        "view",
        "text",
        "series_index",
        "has_epub",
        "title",
        "author_name",
        "series_name",
        "source",
        "tags",
        "keyword",
        "is_favourite",
        "is_special_follow",
    }
)


def parse_search_keyword(keyword: str) -> SearchConditions:
    """Parse a search keyword string into an ordered list of ``(type, value)`` pairs."""
    conditions: SearchConditions = []
    if not keyword or not keyword.strip():
        return conditions

    for cond in keyword.replace("；", ";").split(";"):
        cond = cond.strip()
        if not cond:
            continue
        colon_idx = cond.find(":")
        if colon_idx > 0:
            qtype = cond[:colon_idx].strip()
            if qtype in VALID_DSL_FIELDS:
                qvalue = cond[colon_idx + 1 :].strip()
                if qvalue:
                    conditions.append((qtype, qvalue))
                continue
        qtype = "id" if _BARE_ID_RE.match(cond) else "keyword"
        conditions.append((qtype, cond))
    return conditions


def normalize_search_segments(keyword: str) -> list[str]:
    """Split *keyword* on whitespace, dropping segments with no letter/digit."""
    if not keyword:
        return []
    return [segment for segment in keyword.split() if any(ch.isalpha() or ch.isnumeric() for ch in segment)]


class QuerySpec(BaseModel):
    """Complete read-query specification for novel listing and counting."""

    conditions: SearchConditions = Field(default_factory=list)
    order_by: str = OrderBy.LIKE
    order_direction: str = OrderDirection.DESC
    cursor: dict[str, Any] | None = None
    per_page: int = 50
    min_like: int | None = None
    min_text: int | None = None
    exclude_ids: list[int] = Field(default_factory=list)
    exclude_blocked_tags: bool | None = None


EXCLUDE_BLOCKED_SETTING_KEY = "exclude_blocked_tag_novels"
DEFAULT_ACTIVE = True
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def parse_bool_setting(raw: str | None, *, default: bool = False) -> bool:
    """Parse a boolean setting string; missing or unparsable values return *default*."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return default


def resolve_exclude_blocked(
    explicit: bool | None,
    global_setting_value: str | None,
) -> bool:
    """Decide whether blocked-tag exclusion applies to a query."""
    if explicit is not None:
        return explicit
    return parse_bool_setting(global_setting_value, default=DEFAULT_ACTIVE)


__all__ = [
    "DEFAULT_ACTIVE",
    "EXCLUDE_BLOCKED_SETTING_KEY",
    "OrderBy",
    "OrderDirection",
    "QuerySpec",
    "SearchConditions",
    "SortableOrderBy",
    "VALID_DSL_FIELDS",
    "normalize_search_segments",
    "parse_bool_setting",
    "parse_search_keyword",
    "resolve_exclude_blocked",
]
