"""Protocol and DTOs for the in-process novel and tag query engine adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from copixiv.core.snapshot_types import SlimNovelTuple, SlimTagTuple


class PendingDeltaProtocol(Protocol):
    novel_ids: set[int]
    author_ids: set[int]
    tag_names: set[str]
    tags_full_refresh: bool
    aliases_dirty: bool
    blocked_tags_dirty: bool
    exclude_setting_dirty: bool

    def is_empty(self) -> bool: ...


class SnapshotDeltaProtocol(Protocol):
    @property
    def delete_ids(self) -> list[int]: ...
    @property
    def upsert_rows(self) -> list[SlimNovelTuple]: ...
    @property
    def blocked_tags(self) -> list[str] | None: ...
    @property
    def exclude_blocked_default(self) -> bool | None: ...
    @property
    def special_follow_changes(self) -> list[tuple[int, bool]]: ...
    @property
    def replace_all_tags(self) -> list[SlimTagTuple] | None: ...
    @property
    def delete_tag_names(self) -> list[str] | None: ...
    @property
    def upsert_tags(self) -> list[SlimTagTuple] | None: ...
    @property
    def aliased_tag_ids(self) -> list[int] | None: ...


class SnapshotStoreProtocol(Protocol):
    """Data-layer snapshot/delta store injected by the composition root."""

    def stream_snapshot(
        self,
        consume_total: Callable[[int], None],
        consume_batch: Callable[[list[SlimNovelTuple]], None],
        batch_size: int = 2_000,
    ) -> tuple[list[str], bool, list[int], list[SlimTagTuple], list[int]]: ...

    def fetch_delta_payload(
        self,
        pending: PendingDeltaProtocol,
    ) -> SnapshotDeltaProtocol: ...

    def attach_runtime(
        self,
        engine: Any,
        hook: Callable[[PendingDeltaProtocol], None],
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


class TagCandidateProtocol(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def name(self) -> str: ...
    @property
    def reference_count(self) -> int: ...


class TagSuggestItemProtocol(Protocol):
    @property
    def target(self) -> TagCandidateProtocol: ...
    @property
    def candidates(self) -> list[TagCandidateProtocol]: ...


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

    def ready(self) -> bool: ...

    def needs_rebuild(self) -> bool: ...

    def mark_needs_rebuild(self) -> None: ...

    def swap_from_staging(
        self,
        staging: Any,
        blocked_tags: list[str],
        exclude_blocked_default: bool,
        special_follow_authors: list[int],
        tags: list[SlimTagTuple] | None = None,
        aliased_tag_ids: list[int] | None = None,
    ) -> None: ...

    def apply_delta(
        self,
        *,
        delete_ids: Sequence[int] = (),
        upsert_rows: Iterable[SlimNovelTuple] = (),
        blocked_tags: Sequence[str] | None = None,
        exclude_blocked_default: bool | None = None,
        special_follow_changes: Sequence[tuple[int, bool]] = (),
        replace_all_tags: Sequence[SlimTagTuple] | None = None,
        delete_tag_names: Sequence[str] | None = None,
        upsert_tags: Sequence[SlimTagTuple] | None = None,
        aliased_tag_ids: Sequence[int] | None = None,
    ) -> None: ...

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

    def suggest_tag_aliases(
        self,
        *,
        limit: int = 5,
        offset: int = 0,
        target_tag: str | None = None,
    ) -> tuple[list[TagSuggestItemProtocol], int]: ...
