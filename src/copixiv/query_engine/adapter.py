"""PyO3 ``copixiv_rs`` adapter implementing ``NovelQueryEnginePort``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from copixiv.core.exceptions import ValidationError
from copixiv.core.search_text import SlimNovelTuple
from copixiv.query_engine.port import (
    EngineCountResult,
    EngineCursor,
    EngineMode,
    EngineNovelRecord,
    EnginePageResult,
    EnginePageRowsResult,
    EngineQuery,
)

if TYPE_CHECKING:
    import copixiv_rs


def _rust_module():
    try:
        import copixiv_rs
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "copixiv_rs native extension is not installed. "
            "Build it with `maturin develop --release --manifest-path crates/copixiv_rs/Cargo.toml` "
            "or set query_engine='pg' (COPIXIV_QUERY_ENGINE=pg)."
        ) from exc
    return copixiv_rs


def _to_rust_filter(ext, q: EngineQuery) -> copixiv_rs.QueryFilter:
    return ext.QueryFilter(
        tags=list(q.tags),
        keyword_segments=list(q.keyword_segments),
        int_filters=list(q.int_filters),
        str_filters=list(q.str_filters),
        only_favourite=q.only_favourite,
        only_special_follow=q.only_special_follow,
        min_like=q.min_like,
        min_text=q.min_text,
        exclude_blocked_tags=q.exclude_blocked_tags,
        count_blocked=q.count_blocked,
        restrict_ids=list(q.restrict_ids) if q.restrict_ids is not None else None,
        exclude_ids=list(q.exclude_ids) if q.exclude_ids is not None else None,
    )


class RustNovelQueryEngine:
    """Thread-safe wrapper around ``copixiv_rs.NovelQueryEngine``."""

    def __init__(self, mode: EngineMode = "rust") -> None:
        self._mode: EngineMode = mode
        self._ext = _rust_module()
        self._rs = self._ext.NovelQueryEngine()

    @property
    def mode(self) -> EngineMode:
        return self._mode

    def ready(self) -> bool:
        return self._rs.ready()

    def stats(self) -> tuple[int, int, int, bool]:
        return self._rs.stats()

    def create_staging(self, capacity: int = 0) -> copixiv_rs.StagingEngine:
        return self._ext.StagingEngine(capacity=capacity)

    def swap_from_staging(
        self,
        staging: copixiv_rs.StagingEngine,
        blocked_tags: list[str],
        exclude_blocked_default: bool,
        special_follow_authors: list[int],
    ) -> None:
        self._rs.swap_from_staging(
            staging,
            blocked_tags,
            exclude_blocked_default,
            special_follow_authors,
        )

    def upsert_batch(self, rows: Iterable[SlimNovelTuple]) -> None:
        self._rs.upsert_batch(list(rows))

    def delete_ids(self, novel_ids: Sequence[int]) -> None:
        self._rs.delete_ids(list(novel_ids))

    def toggle_special_follow(self, author_id: int, is_special_follow: bool) -> None:
        self._rs.toggle_special_follow(author_id, is_special_follow)

    def set_blocked_tags(self, blocked_tags: Sequence[str]) -> None:
        self._rs.set_blocked_tags(list(blocked_tags))

    def set_exclude_blocked_default(self, value: bool) -> None:
        self._rs.set_exclude_blocked_default(value)

    def _call_paged(
        self,
        method_name: str,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None,
    ):
        fn = getattr(self._rs, method_name)
        try:
            items, has_more, rand_tuple = fn(
                _to_rust_filter(self._ext, query),
                order_by=order_by or "none",
                descending=descending,
                limit=limit,
                cursor_int=cursor.cursor_int if cursor is not None else None,
                cursor_str=cursor.cursor_str if cursor is not None else None,
                cursor_id=cursor.cursor_id if cursor is not None else None,
                random_seed_hex=cursor.random_seed_hex if cursor is not None else None,
                random_h_hex=cursor.random_h_hex if cursor is not None else None,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        rand_cursor = None
        if rand_tuple is not None:
            seed_hex, h_hex, last_id = rand_tuple
            rand_cursor = {"seed": seed_hex, "h": h_hex, "id": last_id}
        return items, has_more, rand_cursor

    def query_novels(
        self,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None = None,
    ) -> EnginePageResult:
        ids, has_more, rand_cursor = self._call_paged(
            "query_novels",
            query,
            order_by=order_by,
            descending=descending,
            limit=limit,
            cursor=cursor,
        )
        return EnginePageResult(ids=ids, has_more=has_more, random_cursor=rand_cursor)

    def query_novel_rows(
        self,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None = None,
    ) -> EnginePageRowsResult:
        rows, has_more, rand_cursor = self._call_paged(
            "query_novel_rows",
            query,
            order_by=order_by,
            descending=descending,
            limit=limit,
            cursor=cursor,
        )
        return EnginePageRowsResult(
            rows=list(rows),
            has_more=has_more,
            random_cursor=rand_cursor,
        )

    def get_novel_rows_by_ids(self, novel_ids: list[int]) -> list[EngineNovelRecord]:
        return list(self._rs.get_novel_rows_by_ids(novel_ids))

    def count_novels(self, query: EngineQuery, *, cap: int) -> EngineCountResult:
        matched, truncated = self._rs.count_novels(_to_rust_filter(self._ext, query), cap)
        return EngineCountResult(count=matched, truncated=truncated)

    def has_match(self, query: EngineQuery) -> bool:
        return self._rs.has_match(_to_rust_filter(self._ext, query))

    def list_matching_ids(self, query: EngineQuery) -> list[int]:
        return self._rs.list_matching_ids(_to_rust_filter(self._ext, query))

    def list_blocked_ids(self) -> list[int]:
        return self._rs.list_blocked_ids()

    def sort_ids(
        self,
        novel_ids: list[int],
        *,
        order_by: str,
        descending: bool,
    ) -> list[int]:
        try:
            return self._rs.sort_ids(novel_ids, order_by=order_by, descending=descending)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
