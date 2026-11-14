"""Layer 1 unit tests: keyword search parsing, LIKE escaping, and tag normalization."""

from __future__ import annotations

from copixiv.core.services import normalize_tag, parse_search_keyword, parse_tags
from copixiv.features.novels.search import (
    escape_like,
    keyword_condition,
    keyword_params,
    like_predicate,
    normalize_segments,
)


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


def test_escape_like_and_keyword_predicates():
    assert escape_like(r"100%_a\b") == r"100\%\_a\\b"

    assert normalize_segments("--- ... !!") == []
    assert like_predicate("--- ...") is None
    assert keyword_condition("--- ...") is None

    assert normalize_segments("哈利 波特 ---") == ["哈利", "波特"]
    assert like_predicate("哈利 波特") == (
        "search_lower LIKE lower(:p0) ESCAPE '\\' AND "
        "search_lower LIKE lower(:p1) ESCAPE '\\'"
    )
    assert keyword_params("BanG_Dream! %100") == {
        "p0": r"%BanG\_Dream!%",
        "p1": r"%\%100%",
    }


def test_parse_tags_normalizes_splits_and_deduplicates_in_order():
    assert parse_tags([]) == []
    assert parse_tags([None, "", "  ", {"name": ""}]) == []

    tags = parse_tags([
        "  Sci-Fi (SF) ",
        {"name": "R-18/中文|百合#校园、日常\\短篇"},
        "sci-fi sf",
        "百合",
        "ALPHA",
    ])
    assert tags == ["sci-fi sf", "r-18", "中文", "百合", "校园", "日常", "短篇", "alpha"]
    assert normalize_tag("  FooBar ") == "foobar"
