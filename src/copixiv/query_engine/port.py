"""Protocol and DTOs for the in-process novel query engine adapter."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from copixiv.core.search_text import SlimNovelTuple

EngineMode = Literal["rust", "pg", "shadow"]


class PendingDeltaProtocol(Protocol):
    upsert_novel_ids: set[int]
    deleted_novel_ids: set[int]
    special_follow_changes: dict[int, bool]
    blocked_tags_dirty: bool
    exclude_setting_dirty: bool

    def is_empty(self) -> bool: ...


class SnapshotStoreProtocol(Protocol):
    """Data-layer snapshot/delta store injected by the composition root."""

    def stream_snapshot(
        self,
        consume_total: Callable[[int], None],
        consume_batch: Callable[[list[SlimNovelTuple]], None],
        batch_size: int = 25_000,
    ) -> tuple[list[str], bool, list[int]]: ...

    def fetch_delta_payload(
        self,
        *,
        upsert_ids: Sequence[int],
        fetch_blocked: bool,
        fetch_exclude: bool,
    ) -> tuple[list[SlimNovelTuple], list[str] | None, bool | None]: ...

    def attach_runtime(
        self,
        engine: Any,
        hook: Callable[[Any], None],
    ) -> None: ...

    def detach_runtime(self) -> None: ...


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


class EngineNovelRecord(Protocol):
    """Typed attribute view matching ``copixiv_rs.NovelRecord``."""

    @property
    def id(self) -> int: ...
    @property
    def title(self) -> str: ...
    @property
    def author_id(self) -> int | None: ...
    @property
    def author_name(self) -> str | None: ...
    @property
    def series_id(self) -> int | None: ...
    @property
    def series_name(self) -> str | None: ...
    @property
    def series_index(self) -> int | None: ...
    @property
    def text(self) -> int: ...
    @property
    def like(self) -> int: ...
    @property
    def view(self) -> int: ...
    @property
    def has_epub(self) -> int: ...
    @property
    def create_time_us(self) -> int | None: ...
    @property
    def source(self) -> str: ...
    @property
    def is_favourite(self) -> bool: ...
    @property
    def is_special_follow(self) -> bool: ...
    @property
    def tags(self) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class EnginePageResult:
    """Result of ``query_novels``: ordered page IDs + pagination flags."""

    ids: list[int]
    has_more: bool
    random_cursor: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EnginePageRowsResult:
    """Result of ``query_novel_rows``: materialized rows + pagination flags."""

    rows: list[EngineNovelRecord]
    has_more: bool
    random_cursor: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EngineCountResult:
    """Bounded count result matching PG ``NovelCount`` semantics."""

    count: int
    truncated: bool


class NovelQueryEnginePort(Protocol):
    """Port implemented by ``RustNovelQueryEngine``."""

    @property
    def mode(self) -> EngineMode: ...

    def ready(self) -> bool: ...

    def query_novels(
        self,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None = None,
    ) -> EnginePageResult: ...

    def query_novel_rows(
        self,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None = None,
    ) -> EnginePageRowsResult: ...

    def get_novel_rows_by_ids(self, novel_ids: list[int]) -> list[EngineNovelRecord]: ...

    def count_novels(self, query: EngineQuery, *, cap: int) -> EngineCountResult: ...

    def has_match(self, query: EngineQuery) -> bool: ...

    def list_matching_ids(self, query: EngineQuery) -> list[int]: ...

    def list_blocked_ids(self) -> list[int]: ...

    def sort_ids(
        self,
        novel_ids: list[int],
        *,
        order_by: str,
        descending: bool,
    ) -> list[int]: ...
