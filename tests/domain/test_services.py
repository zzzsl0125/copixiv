"""Pure unit tests for domain services — zero I/O."""

import io

import pytest

from copixiv.domain.services.tags import parse_tags, normalize_tag
from copixiv.domain.services.language import has_image_placeholders
from copixiv.domain.services.filename import safe_filename, build_path
from copixiv.domain.services.parsing import (
    safe_get,
    safe_set,
    guess_series_order,
    parse_search_keyword,
)
from copixiv.domain.services.archive import build_batch_zip


# ---- tag parsing ----

class TestParseTags:
    def test_string_tags(self):
        assert set(parse_tags(["a", "b"])) == {"a", "b"}

    def test_dict_tags(self):
        result = parse_tags([{"name": "R-18"}, {"name": "NTR"}])
        assert set(result) == {"r-18", "ntr"}

    def test_split_on_delimiters(self):
        result = parse_tags(["猫|犬", "A/B"])
        assert set(result) == {"猫", "犬", "a", "b"}

    def test_remove_parentheses(self):
        result = parse_tags(["Hello(World)"])
        assert result == ["helloworld"]

    def test_deduplicate(self):
        result = parse_tags(["a", "a", "A"])
        assert result == ["a"]

    def test_strips_whitespace(self):
        result = parse_tags(["  hello  "])
        assert result == ["hello"]


class TestNormalizeTag:
    def test_lowercase(self):
        assert normalize_tag("NTR") == "ntr"

    def test_strip(self):
        assert normalize_tag("  tag  ") == "tag"


# ---- language detection (no I/O parts) ----

class TestHasImagePlaceholders:
    def test_uploaded_image(self):
        assert has_image_placeholders("[uploadedimage:12345]") is True

    def test_pixiv_image(self):
        assert has_image_placeholders("[pixivimage:67890-1]") is True

    def test_no_placeholder(self):
        assert has_image_placeholders("plain text") is False

    def test_empty_string(self):
        assert has_image_placeholders("") is False


# ---- filename utils ----

class TestSafeFilename:
    def test_preserves_valid_text(self):
        assert safe_filename("Hello World") == "Hello World"

    def test_strips_illegal_chars(self):
        name = safe_filename('file:name?<test>')
        assert ":" not in name
        assert "?" not in name
        assert "<" not in name
        assert ">" not in name

    def test_returns_untitled_for_empty(self):
        assert safe_filename("   ") == "untitled"

    def test_truncates_long_utf8(self):
        long_name = "あ" * 300
        result = safe_filename(long_name)
        assert len(result.encode("utf-8")) <= 240

    def test_does_not_split_multibyte(self):
        # Ensure we don't leave a partial UTF-8 sequence at the boundary
        name = "测试" * 100
        result = safe_filename(name)
        # Must decode cleanly
        result.encode("utf-8").decode("utf-8")


class TestBuildPath:
    def test_small_id(self):
        p = build_path(42, "My Novel", "download")
        assert p.startswith("download/0000/")
        assert p.endswith("_42.txt")

    def test_large_id(self):
        p = build_path(12345678, "Big Novel", "dl")
        assert p.startswith("dl/1234/")

    def test_sanitized_title(self):
        p = build_path(1, 'bad:name*', "d")
        assert ":" not in p
        assert "*" not in p


# ---- parsing ----

class TestSafeGet:
    def test_from_dict(self):
        assert safe_get({"a": 1}, "a") == 1
        assert safe_get({"a": 1}, "b") is None
        assert safe_get({"a": 1}, "b", 0) == 0

    def test_from_none(self):
        assert safe_get(None, "a") is None

    class _Obj:
        a = 5

    def test_from_object(self):
        assert safe_get(self._Obj(), "a") == 5


class TestSafeSet:
    def test_on_dict(self):
        d = {"a": 1}
        safe_set(d, "b", 2)
        assert d["b"] == 2


class TestGuessSeriesOrder:
    def test_prev_novel(self):
        nav = type("nav", (), {"prevNovel": type("p", (), {"contentOrder": 4})()})()
        assert guess_series_order(nav) == 5

    def test_next_novel(self):
        nav = type("nav", (), {"nextNovel": type("p", (), {"contentOrder": 6})()})()
        assert guess_series_order(nav) == 5

    def test_none(self):
        assert guess_series_order(None) is None

    def test_empty(self):
        nav = type("nav", (), {})()
        assert guess_series_order(nav) is None


class TestParseSearchKeyword:
    def test_simple_keyword(self):
        assert parse_search_keyword("R-18") == {"R-18": "keyword"}

    def test_typed_conditions(self):
        result = parse_search_keyword("keyword:R-18;author_id:12345")
        assert result == {"R-18": "keyword", "12345": "author_id"}

    def test_chinese_semicolon(self):
        result = parse_search_keyword("tag:NTR；keyword:abc")
        assert result == {"NTR": "tag", "abc": "keyword"}

    def test_empty(self):
        assert parse_search_keyword("") == {}
        assert parse_search_keyword("   ") == {}

    def test_empty_segment(self):
        result = parse_search_keyword("a;;b")
        assert result == {"a": "keyword", "b": "keyword"}


# ---- archive ----

class TestBuildBatchZip:
    def test_empty_list(self):
        buf, titles, missing = build_batch_zip([])
        assert len(titles) == 0
        assert len(missing) == 0

    def test_missing_path(self):
        novels = [{"id": 1, "path": None, "title": "T", "author_name": "A"}]
        buf, titles, missing = build_batch_zip(novels)
        assert missing == ["1"]

    def test_file_not_on_disk(self):
        novels = [{"id": 99999, "path": "/nonexistent/file.txt", "title": "T", "author_name": "A"}]
        buf, titles, missing = build_batch_zip(novels)
        assert "99999" in missing
