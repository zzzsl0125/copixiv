"""Layer 1 unit tests: keyword search parsing, segment normalization, and tag normalization."""

from __future__ import annotations

import pytest

from copixiv.core.exceptions import ValidationError
from copixiv.core.search import normalize_search_segments, parse_search_keyword
from copixiv.core.text import normalize_tag, parse_tags, validate_tags


def test_parse_search_keyword_combines_typed_and_bare_conditions():
    raw = "tags:A;tags:B；author_name:Alice;series_name:Saga;id:123;author_id:456;source:local;hello;1234567"
    assert parse_search_keyword(raw) == [
        ("tags", "A"),
        ("tags", "B"),
        ("author_name", "Alice"),
        ("series_name", "Saga"),
        ("id", "123"),
        ("author_id", "456"),
        ("source", "local"),
        ("keyword", "hello"),
        ("id", "1234567"),
    ]


def test_parse_search_keyword_empty_and_punctuation():
    assert parse_search_keyword("") == []
    assert parse_search_keyword("   ; ； ") == []
    assert parse_search_keyword("tags:") == []
    assert parse_search_keyword("123456") == [("keyword", "123456")]


def test_normalize_search_segments_drops_punctuation_only():
    assert normalize_search_segments("") == []
    assert normalize_search_segments("--- ... !!") == []
    assert normalize_search_segments("哈利 波特 ---") == ["哈利", "波特"]
    assert normalize_search_segments("BanG_Dream! %100") == ["BanG_Dream!", "%100"]


def test_parse_tags_normalizes_splits_and_deduplicates_in_order():
    assert parse_tags([]) == []
    assert parse_tags([None, "", "  ", {"name": ""}]) == []

    tags = parse_tags(
        [
            "  Sci-Fi (SF) ",
            {"name": "R-18/中文|百合#校园、日常\\短篇"},
            "sci-fi sf",
            "百合",
            "ALPHA",
        ]
    )
    assert tags == ["sci-fi sf", "r-18", "中文", "百合", "校园", "日常", "短篇", "alpha"]
    assert normalize_tag("  FooBar ") == "foobar"


def test_validate_tags_enforces_empty_count_and_length_bounds():
    with pytest.raises(ValidationError, match="请至少输入一个标签"):
        validate_tags([" ", None], require_non_empty=True)
    with pytest.raises(ValidationError, match="一次最多操作 2 个标签"):
        validate_tags(["a", "b", "c"], max_count=2)
    with pytest.raises(ValidationError, match="标签过长"):
        validate_tags(["abcdef"], max_length=5)
    assert validate_tags(["  Alpha ", "beta/ALPHA"], max_count=5) == ["alpha", "beta"]
