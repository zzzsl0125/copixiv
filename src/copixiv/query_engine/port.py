"""DTOs for the in-process novel and tag query engine adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import copixiv_rs


@dataclass(frozen=True, slots=True)
class EngineQuery:
    """Validated filter specification passed from repositories to the query engine."""

    tags: tuple[str, ...] = ()
    keyword_segments: tuple[str, ...] = ()
    int_filters: tuple[tuple[str, int], ...] = ()
    str_filters: tuple[tuple[str, str], ...] = ()
    only_favourite: bool = False
    only_special_follow: bool = False
    min_like: int | None = None
    min_text: int | None = None
    exclude_blocked_tags: bool | None = None
    count_blocked: bool = False
    restrict_ids: tuple[int, ...] | None = None
    exclude_ids: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class EngineCursor:
    """Typed keyset cursor for deterministic or stateless random pagination."""

    cursor_id: int
    cursor_int: int | None = None
    cursor_str: str | None = None
    random_seed_hex: str | None = None
    random_h_hex: str | None = None


@dataclass(frozen=True, slots=True)
class EnginePageRowsResult:
    """Result of ``query_novel_rows``: materialized rows + pagination flags."""

    rows: list[copixiv_rs.NovelRecord]
    has_more: bool
    random_cursor: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EngineCountResult:
    """Bounded count result matching PG ``NovelCount`` semantics."""

    count: int
    truncated: bool


__all__ = [
    "EngineCountResult",
    "EngineCursor",
    "EnginePageRowsResult",
    "EngineQuery",
]
