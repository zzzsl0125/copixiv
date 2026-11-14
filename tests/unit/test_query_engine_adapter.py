"""Unit tests for the in-process Rust query engine adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from copixiv.core.search_text import (
    SlimNovelTuple,
    datetime_to_us,
    normalize_search_segments,
    us_to_datetime,
)
from copixiv.query_engine.adapter import RustNovelQueryEngine
from copixiv.query_engine.port import EngineCursor, EngineQuery


def _row(
    novel_id: int,
    *,
    title: str = "Untitled",
    author_id: int | None = 1,
    author_name: str | None = "Author",
    series_id: int | None = None,
    series_name: str | None = None,
    series_index: int | None = None,
    text: int = 1000,
    like: int = 10,
    view: int = 100,
    has_epub: int = 0,
    create_time_us: int | None = 1_700_000_000_000_000,
    source: str = "pixiv",
    is_favourite: bool = False,
    is_special_follow: bool = False,
    tags: list[str] | None = None,
) -> SlimNovelTuple:
    return (
        novel_id,
        title,
        author_id,
        author_name,
        series_id,
        series_name,
        series_index,
        text,
        like,
        view,
        has_epub,
        create_time_us,
        source,
        is_favourite,
        is_special_follow,
        tags or [],
    )


def _build_engine(
    rows: list[SlimNovelTuple],
    *,
    blocked_tags: list[str] | None = None,
    exclude_default: bool = True,
    sf_authors: list[int] | None = None,
) -> RustNovelQueryEngine:
    engine = RustNovelQueryEngine(mode="rust")
    staging = engine.create_staging(capacity=len(rows))
    staging.push_batch(rows)
    engine.swap_from_staging(
        staging,
        blocked_tags or [],
        exclude_default,
        sf_authors or [],
    )
    return engine


def test_datetime_to_us_precision_and_timezone_normalization():
    utc_dt = datetime(2025, 1, 13, 15, 0, 0, 123456, tzinfo=timezone.utc)
    us = datetime_to_us(utc_dt)
    assert us == 1736780400 * 1_000_000 + 123456
    assert us_to_datetime(us) == utc_dt
    plus8 = datetime(2025, 1, 13, 23, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    assert datetime_to_us(plus8) == us
    assert us_to_datetime(datetime_to_us(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc))) == (
        datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)
    )
    assert datetime_to_us(None) is None
    assert us_to_datetime(None) is None


def test_ascii_only_case_fold_cjk_and_empty_segments():
    assert normalize_search_segments("--- !!!") == []
    assert normalize_search_segments("Hello  Ñ\t原神") == ["Hello", "Ñ", "原神"]

    rows = [
        _row(1, title="Hello Ñ Hero", author_name="Bob", tags=["原神", "A"]),
        _row(2, title="hello ñ hero", author_name="Zed", tags=["原神"]),
        _row(3, title="Star", author_name="Bob", series_name="Arc", tags=["R-18"]),
    ]
    eng = _build_engine(rows)

    # C-locale parity: ASCII case folds, non-ASCII 'Ñ' stays exact.
    assert eng.list_matching_ids(EngineQuery(keyword_segments=("hello", "Ñ"))) == [1]
    assert eng.list_matching_ids(EngineQuery(keyword_segments=("HERO",))) == [1, 2]
    assert eng.list_matching_ids(EngineQuery(keyword_segments=("原",))) == [1, 2]
    # 1-byte ASCII query
    assert eng.list_matching_ids(EngineQuery(keyword_segments=("a",))) == [1, 3]
    # Joined fields match series_name + tags together
    assert eng.list_matching_ids(EngineQuery(keyword_segments=("bob", "arc", "r-18"))) == [3]


def test_abs_id_null_sort_order_and_js_safe_random_cursor():
    rows = [
        _row(-200, title="Local200", like=10, create_time_us=None, source="local"),
        _row(100, title="Pixiv100", like=20, create_time_us=1000),
        _row(200, title="Pixiv200", like=20, create_time_us=None),
        _row(300, title="Pixiv300", like=50, create_time_us=5000),
    ]
    eng = _build_engine(rows)
    q = EngineQuery()

    # abs(id) DESC -> 300, +200, -200, 100
    assert eng.query_novels(q, order_by="id", descending=True, limit=10).ids == [300, 200, -200, 100]
    assert eng.query_novels(q, order_by="id", descending=False, limit=10).ids == [100, -200, 200, 300]

    # PG NULLS FIRST on DESC, NULLS LAST on ASC for create_time
    assert eng.query_novels(
        q, order_by="create_time", descending=True, limit=10,
    ).ids == [200, -200, 300, 100]
    assert eng.query_novels(
        q, order_by="create_time", descending=False, limit=10,
    ).ids == [100, 300, -200, 200]

    # Non-null create_time cursor excludes NULL rows in both directions
    assert eng.query_novels(
        q,
        order_by="create_time",
        descending=True,
        limit=10,
        cursor=EngineCursor(cursor_id=300, cursor_int=5000),
    ).ids == [100]
    assert eng.query_novels(
        q,
        order_by="create_time",
        descending=False,
        limit=10,
        cursor=EngineCursor(cursor_id=100, cursor_int=1000),
    ).ids == [300]

    # Stateless true-random walk produces hex cursors (JS safe) and covers all 4 rows cleanly.
    seen: list[int] = []
    cur: EngineCursor | None = None
    while True:
        page = eng.query_novels(q, order_by="random", descending=True, limit=2, cursor=cur)
        seen.extend(page.ids)
        if not page.has_more:
            assert page.random_cursor is None
            break
        rc = page.random_cursor
        assert rc is not None
        assert isinstance(rc["seed"], str) and len(rc["seed"]) == 16
        assert isinstance(rc["h"], str) and len(rc["h"]) == 16
        assert isinstance(rc["id"], int)
        cur = EngineCursor(
            cursor_id=rc["id"],
            random_seed_hex=rc["seed"],
            random_h_hex=rc["h"],
        )
    assert len(seen) == 4 and set(seen) == {-200, 100, 200, 300}

    # Direct row materialization matches query_novels order & preserves all attributes.
    page_rows = eng.query_novel_rows(q, order_by="id", descending=True, limit=2)
    assert [r.id for r in page_rows.rows] == [300, 200]
    assert page_rows.has_more is True
    by_ids = eng.get_novel_rows_by_ids([-200, 999, 300])
    assert [(r.id, r.title, r.source, r.create_time_us) for r in by_ids] == [
        (-200, "Local200", "local", None),
        (300, "Pixiv300", "pixiv", 5000),
    ]
