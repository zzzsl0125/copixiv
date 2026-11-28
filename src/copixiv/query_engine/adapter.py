"""PyO3 ``copixiv_rs`` adapter for the in-process novel and tag query engine."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from copixiv.core.exceptions import ValidationError
from copixiv.core.snapshot_types import SlimNovelRecord
from copixiv.query_engine.port import (
    EngineCountResult,
    EngineCursor,
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
            "Build it with `maturin develop --release --manifest-path crates/copixiv_rs/Cargo.toml`."
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

    def __init__(self) -> None:
        self._ext = _rust_module()
        self._rs = self._ext.NovelQueryEngine()

    def ready(self) -> bool:
        return self._rs.ready()

    def needs_rebuild(self) -> bool:
        return self._rs.needs_rebuild()

    def mark_needs_rebuild(self) -> None:
        self._rs.mark_needs_rebuild()

    def stats(self) -> tuple[int, int, int, bool, int, bool]:
        return self._rs.stats()

    def memory_breakdown(self) -> list[tuple[str, int, int]]:
        return self._rs.memory_breakdown()

    def create_staging(self, capacity: int = 0) -> copixiv_rs.StagingEngine:
        return self._ext.StagingEngine(capacity=capacity)

    def swap_from_staging(
        self,
        staging: copixiv_rs.StagingEngine,
        blocked_tags: list[str],
        exclude_blocked_default: bool,
        aliased_tags: list[str] | None = None,
    ) -> None:
        self._rs.swap_from_staging(
            staging,
            blocked_tags,
            exclude_blocked_default,
            list(aliased_tags) if aliased_tags is not None else [],
        )

    def apply_delta(
        self,
        *,
        delete_ids: Sequence[int] = (),
        delete_series_ids: Sequence[int] = (),
        upsert_rows: Iterable[SlimNovelRecord] = (),
        blocked_tags: Sequence[str] | None = None,
        exclude_blocked_default: bool | None = None,
        special_follow_changes: Sequence[tuple[int, bool]] = (),
        aliased_tags: Sequence[str] | None = None,
    ) -> None:
        self._rs.apply_delta(
            delete_ids=list(delete_ids),
            delete_series_ids=list(delete_series_ids),
            upsert_rows=list(upsert_rows),
            blocked_tags=list(blocked_tags) if blocked_tags is not None else None,
            exclude_blocked_default=exclude_blocked_default,
            special_follow_changes=list(special_follow_changes),
            aliased_tags=list(aliased_tags) if aliased_tags is not None else None,
        )

    def query_novel_rows(
        self,
        query: EngineQuery,
        *,
        order_by: str,
        descending: bool,
        limit: int,
        cursor: EngineCursor | None = None,
    ) -> EnginePageRowsResult:
        try:
            rows, has_more, rand_tuple = self._rs.query_novel_rows(
                _to_rust_filter(self._ext, query),
                order_by=order_by or "none",
                descending=descending,
                limit=limit,
                cursor_int=cursor.cursor_int if cursor is not None else None,
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
        return EnginePageRowsResult(
            rows=list(rows),
            has_more=has_more,
            random_cursor=rand_cursor,
        )

    def get_novel_rows_by_ids(self, novel_ids: list[int]) -> list[copixiv_rs.NovelRecord]:
        return list(self._rs.get_novel_rows_by_ids(novel_ids))

    def count_novels(self, query: EngineQuery, *, cap: int) -> EngineCountResult:
        matched, truncated = self._rs.count_novels(_to_rust_filter(self._ext, query), cap)
        return EngineCountResult(count=matched, truncated=truncated)

    def has_match(self, query: EngineQuery) -> bool:
        return self._rs.has_match(_to_rust_filter(self._ext, query))

    def list_matching_ids(self, query: EngineQuery) -> list[int]:
        return self._rs.list_matching_ids(_to_rust_filter(self._ext, query))

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

    def suggest_tag_aliases(
        self,
        *,
        limit: int = 5,
        offset: int = 0,
        target_tag: str | None = None,
    ) -> tuple[list[copixiv_rs.TagSuggestItem], int | None]:
        items, next_cursor = self._rs.suggest_tag_aliases(
            limit=limit,
            offset=offset,
            target_tag=target_tag,
        )
        return list(items), (int(next_cursor) if next_cursor is not None else None)
