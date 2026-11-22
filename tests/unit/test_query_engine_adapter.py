"""Unit tests for the in-process Rust query engine adapter."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest

from copixiv.core.search import normalize_search_segments
from copixiv.core.snapshot_types import SlimNovelRecord
from copixiv.core.time import datetime_to_us, us_to_datetime
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
) -> SlimNovelRecord:
    return SlimNovelRecord(
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
    rows: list[SlimNovelRecord],
    *,
    blocked_tags: list[str] | None = None,
    exclude_default: bool = True,
) -> RustNovelQueryEngine:
    engine = RustNovelQueryEngine()
    staging = engine.create_staging(capacity=len(rows))
    staging.push_batch(rows)
    engine.swap_from_staging(
        staging,
        blocked_tags or [],
        exclude_default,
    )
    return engine


def test_datetime_to_us_precision_and_timezone_normalization():
    utc_dt = datetime(2025, 1, 13, 15, 0, 0, 123456, tzinfo=UTC)
    us = datetime_to_us(utc_dt)
    assert us == 1736780400 * 1_000_000 + 123456
    assert us_to_datetime(us) == utc_dt
    plus8 = datetime(2025, 1, 13, 23, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    assert datetime_to_us(plus8) == us
    naive_utc = datetime(2025, 1, 13, 15, 0, 0, 123456)
    assert datetime_to_us(naive_utc) == us
    assert us_to_datetime(datetime_to_us(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC))) == (
        datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)
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
    assert [r.id for r in eng.query_novel_rows(q, order_by="id", descending=True, limit=10).rows] == [
        300,
        200,
        -200,
        100,
    ]
    assert [r.id for r in eng.query_novel_rows(q, order_by="id", descending=False, limit=10).rows] == [
        100,
        -200,
        200,
        300,
    ]

    # PG NULLS FIRST on DESC, NULLS LAST on ASC for create_time
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=True,
            limit=10,
        ).rows
    ] == [200, -200, 300, 100]
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=False,
            limit=10,
        ).rows
    ] == [100, 300, -200, 200]

    # Keyset create_time cursor preserves PG NULLS FIRST (DESC) and NULLS LAST (ASC)
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=True,
            limit=10,
            cursor=EngineCursor(cursor_id=300, cursor_int=5000),
        ).rows
    ] == [100]
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=True,
            limit=10,
            cursor=EngineCursor(cursor_id=200, cursor_int=None),
        ).rows
    ] == [-200, 300, 100]
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=False,
            limit=10,
            cursor=EngineCursor(cursor_id=100, cursor_int=1000),
        ).rows
    ] == [300, -200, 200]
    assert [
        r.id
        for r in eng.query_novel_rows(
            q,
            order_by="create_time",
            descending=False,
            limit=10,
            cursor=EngineCursor(cursor_id=-200, cursor_int=None),
        ).rows
    ] == [200]

    # Stateless true-random walk produces hex cursors (JS safe) and covers all 4 rows cleanly.
    seen: list[int] = []
    cur: EngineCursor | None = None
    while True:
        page = eng.query_novel_rows(q, order_by="random", descending=True, limit=2, cursor=cur)
        seen.extend(r.id for r in page.rows)
        if not page.has_more:
            assert page.random_cursor is None
            break
        rc = page.random_cursor
        assert rc is not None
        assert isinstance(rc["seed"], str)
        assert len(rc["seed"]) == 16
        assert isinstance(rc["h"], str)
        assert len(rc["h"]) == 16
        assert isinstance(rc["id"], int)
        cur = EngineCursor(
            cursor_id=rc["id"],
            random_seed_hex=rc["seed"],
            random_h_hex=rc["h"],
        )
    assert len(seen) == 4
    assert set(seen) == {-200, 100, 200, 300}

    # Direct row lookup by IDs preserves caller order & attributes.
    by_ids = eng.get_novel_rows_by_ids([-200, 999, 300])
    assert [(r.id, r.title, r.source, r.create_time_us) for r in by_ids] == [
        (-200, "Local200", "local", None),
        (300, "Pixiv300", "pixiv", 5000),
    ]


def test_tag_suggestions_rebuild_flag_and_memory_breakdown():
    eng = RustNovelQueryEngine()
    stg = eng.create_staging(capacity=2)
    stg.push_batch(
        [
            _row(1, title="Book 1", tags=["百合", "日常"]),
            _row(2, title="Book 2", tags=["百合", "百合漫画"]),
        ]
    )
    eng.swap_from_staging(
        stg,
        [],
        True,
        aliased_tags=[],
    )
    assert eng.needs_rebuild() is False
    items, next_off = eng.suggest_tag_aliases(limit=5, offset=0, target_tag="百合")
    assert next_off is None
    assert len(items) == 1
    assert items[0].target.reference_count == 2
    assert items[0].target.id > 0
    assert [(c.name, c.reference_count) for c in items[0].candidates] == [("百合漫画", 1)]
    breakdown = eng.memory_breakdown()
    assert any(name == "postings_slab" for name, _, _ in breakdown)
    eng.mark_needs_rebuild()
    assert eng.needs_rebuild() is True


def test_lazy_rust_import_allows_pure_python_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    import copixiv.query_engine as query_engine_pkg

    orig_adapter = sys.modules["copixiv.query_engine.adapter"]
    monkeypatch.setitem(sys.modules, "copixiv_rs", None)
    monkeypatch.delitem(sys.modules, "copixiv.query_engine.adapter", raising=False)
    monkeypatch.delitem(sys.modules, "copixiv.export_openapi", raising=False)
    monkeypatch.setattr(query_engine_pkg, "adapter", orig_adapter)

    reloaded_adapter = importlib.import_module("copixiv.query_engine.adapter")
    importlib.import_module("copixiv.export_openapi")

    with pytest.raises(ModuleNotFoundError, match="maturin develop"):
        reloaded_adapter.RustNovelQueryEngine()
