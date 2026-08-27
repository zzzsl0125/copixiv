"""Pure unit tests for domain services — zero I/O."""


import pytest
from pathlib import Path

from copixiv.core.models import Novel

from copixiv.core.services import parse_tags, normalize_tag
from copixiv.core.services import has_image_placeholders
from copixiv.core.services import safe_filename, build_path, NovelNamingTemplate
from copixiv.core.services import (
    safe_get,
    safe_set,
    guess_series_order,
    parse_search_keyword,
)
from copixiv.core.services import build_batch_zip


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

    def test_preserves_input_order(self):
        """Regression: output order follows first-seen input order, not set
        iteration order (the dedup map preserves insertion sequence)."""
        result = parse_tags(["zebra", "alpha", "zebra", "猫", "alpha"])
        assert result == ["zebra", "alpha", "猫"]


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

    def test_basename_fits_name_max_with_suffixes(self):
        """Long titles must leave room for _<id>.txt/.epub plus .tmp."""
        long_title = "长" * 300
        p = build_path(28904936, long_title, "download")
        base = Path(p).name
        assert len(base.encode("utf-8")) <= 255
        # Atomic-write temp variants must fit too (Errno 36 regression).
        for suffix in (".txt.tmp", ".epub.tmp"):
            tmp = base.rsplit(".", 1)[0] + suffix
            assert len(tmp.encode("utf-8")) <= 255

    def test_basename_fits_for_max_id_digits(self):
        long_title = "あ" * 300
        p = build_path(123456789012, long_title, "download")
        base = Path(p).name
        assert len(base.encode("utf-8")) <= 255
        tmp = base.rsplit(".", 1)[0] + ".epub.tmp"
        assert len(tmp.encode("utf-8")) <= 255


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
        assert parse_search_keyword("R-18") == [("keyword", "R-18")]

    def test_typed_conditions(self):
        result = parse_search_keyword("keyword:R-18;author_id:12345")
        assert result == [("keyword", "R-18"), ("author_id", "12345")]

    def test_chinese_semicolon(self):
        result = parse_search_keyword("tag:NTR；keyword:abc")
        assert result == [("tag", "NTR"), ("keyword", "abc")]

    def test_empty(self):
        assert parse_search_keyword("") == []
        assert parse_search_keyword("   ") == []

    def test_empty_segment(self):
        result = parse_search_keyword("a;;b")
        assert result == [("keyword", "a"), ("keyword", "b")]

    def test_order_and_duplicates_preserved(self):
        """The list must keep order and duplicates — the old {value: type}
        dict silently dropped colliding conditions."""
        result = parse_search_keyword("keyword:X;tag:X;keyword:X")
        assert result == [
            ("keyword", "X"), ("tag", "X"), ("keyword", "X"),
        ]

    def test_bare_seven_digit_number_is_id(self):
        assert parse_search_keyword("1285180") == [("id", "1285180")]
        # Shorter numbers stay keywords (they can never be a valid ID).
        assert parse_search_keyword("123456") == [("keyword", "123456")]

    def test_empty_value_segment_skipped(self):
        assert parse_search_keyword("keyword:;R-18") == [("keyword", "R-18")]


# ---- archive ----

class TestBuildBatchZip:
    def test_empty_list(self):
        buf, titles, missing = build_batch_zip([])
        assert len(titles) == 0
        assert len(missing) == 0

    def test_missing_path(self):
        novels = [Novel(id=1, title="T", author_id=0, path=None)]
        buf, titles, missing = build_batch_zip(novels)
        assert missing == ["1"]

    def test_file_not_on_disk(self):
        novels = [Novel(id=99999, title="T", author_id=0,
                        path="/nonexistent/file.txt")]
        buf, titles, missing = build_batch_zip(novels)
        assert "99999" in missing


# ---- naming template ----

def _novel_dict(**overrides: object):
    """Minimal novel-shaped object for template resolution tests.

    A SimpleNamespace (not the strict :class:`Novel` model) so the
    datetime-``create_time`` case stays testable — the template engine
    deliberately works on any attribute-bearing object.
    """
    from types import SimpleNamespace
    return SimpleNamespace(**{
        "id": 12345678,
        "title": "テスト小説",
        "author_name": "作者名",
        "author_id": 99999,
        "like": 100,
        "view": 5000,
        "text": 3000,
        "series_name": "シリーズ",
        "series_index": 3,
        "create_time": "2024-01-15T00:00:00",
        "path": "/some/path",
        "has_epub": 0,
    } | overrides)


class TestNovelNamingTemplate:
    """Unit tests for the token-based template engine."""

    # -- construction --------------------------------------------------

    def test_id_required(self):
        with pytest.raises(ValueError, match="id"):
            NovelNamingTemplate("{title}")

    # -- token resolution ----------------------------------------------

    def test_all_basic_tokens_resolved(self):
        tpl = NovelNamingTemplate("{id}_{title}_{author_name}_{author_id}")
        result = tpl.resolve(_novel_dict())
        assert result == "12345678_テスト小説_作者名_99999"

    def test_stats_tokens_resolved(self):
        tpl = NovelNamingTemplate("{id}_{like}_{view}_{text}")
        result = tpl.resolve(_novel_dict())
        assert result == "12345678_100_5000_3000"

    def test_date_token_formatted(self):
        tpl = NovelNamingTemplate("{id}_{date}")
        # create_time as string "2024-01-15T00:00:00" → "2024-01-15"
        result = tpl.resolve(_novel_dict())
        assert result == "12345678_2024-01-15"

    def test_date_token_none(self):
        tpl = NovelNamingTemplate("{id}_{date}")
        result = tpl.resolve(_novel_dict(create_time=None))
        # empty {date} removes adjacent separator '_'
        assert result == "12345678"

    def test_date_token_datetime_object(self):
        from datetime import datetime
        tpl = NovelNamingTemplate("{id}_{date}")
        result = tpl.resolve(_novel_dict(create_time=datetime(2024, 6, 15)))
        assert result == "12345678_2024-06-15"

    def test_series_tokens_with_series(self):
        tpl = NovelNamingTemplate(
            "{author_name}/{series_name}/#{series_index}_{title}_{id}"
        )
        result = tpl.resolve(_novel_dict())
        assert result == "作者名/シリーズ/#3_テスト小説_12345678"

    def test_series_tokens_without_series(self):
        tpl = NovelNamingTemplate(
            "{author_name}/{series_name}/#{series_index}_{title}_{id}"
        )
        result = tpl.resolve(_novel_dict(series_name=None, series_index=None))
        # /シリーズディレクトリ/#_  should both collapse
        assert result == "作者名/テスト小説_12345678"

    def test_author_name_defaults_to_unknown(self):
        tpl = NovelNamingTemplate("{id}_{author_name}")
        result = tpl.resolve(_novel_dict(author_name=None))
        assert result == "12345678_未知作者"

    # -- sanitization --------------------------------------------------

    def test_illegal_chars_replaced_with_fullwidth(self):
        tpl = NovelNamingTemplate("{id}_{title}")
        result = tpl.resolve(_novel_dict(title='test:file?<name>'))
        assert ":" not in result
        assert "?" not in result
        assert "<" not in result
        assert ">" not in result
        assert "：" in result
        assert "？" in result

    def test_path_separator_in_title_mapped(self):
        """Title containing '/' should have '/' mapped to full-width '／'."""
        tpl = NovelNamingTemplate("{id}_{title}")
        result = tpl.resolve(_novel_dict(title="a/b"))
        assert result == "12345678_a／b"

    def test_backslash_in_title_mapped(self):
        tpl = NovelNamingTemplate("{id}_{title}")
        result = tpl.resolve(_novel_dict(title="a\\b"))
        assert "\\" not in result
        assert "＼" in result

    def test_windows_reserved_plain_name(self):
        """When the entire path segment is a reserved name, append suffix."""
        tpl = NovelNamingTemplate("{id}/{title}")
        result = tpl.resolve(_novel_dict(title="CON"))
        assert result == "12345678/CON[WinReserved]"

    def test_windows_reserved_with_extension(self):
        """When reserved name has a dot extension, replace all dots."""
        tpl = NovelNamingTemplate("{id}/{title}")
        result = tpl.resolve(_novel_dict(title="CON.txt"))
        assert "CON．txt" in result
        assert "." not in result.split("/")[-1]

    def test_leading_dot_stripped(self):
        tpl = NovelNamingTemplate("{id}_{title}")
        result = tpl.resolve(_novel_dict(title=".hidden"))
        assert not result.endswith("/.hidden")

    def test_control_chars_removed(self):
        tpl = NovelNamingTemplate("{id}_{title}")
        result = tpl.resolve(_novel_dict(title="test\x00null"))
        assert "\x00" not in result
        assert "testnull" in result

    # -- separator removal ---------------------------------------------

    def test_empty_series_order_removes_wrapping_separators(self):
        tpl = NovelNamingTemplate("{id}#{series_index}_{title}")
        result = tpl.resolve(_novel_dict(series_index=None))
        # #{series_index}_ — all three chars adjacent to empty token → removed
        assert "#" not in result
        assert result == "12345678テスト小説"

    def test_consecutive_slashes_collapsed(self):
        tpl = NovelNamingTemplate("{author_name}/{series_name}/{id}")
        result = tpl.resolve(_novel_dict(series_name=None))
        assert "//" not in result
        assert result == "作者名/12345678"

    def test_multiple_separators_stripped(self):
        tpl = NovelNamingTemplate("{id}_#-_{title}")
        result = tpl.resolve(_novel_dict())
        assert result == "12345678_#-_テスト小説"

    # -- integration ---------------------------------------------------

    def test_default_template_with_real_dict(self):
        tpl = NovelNamingTemplate(
            "{author_name}/{series_name}/#{series_index}_{title}_{id}"
        )
        novel = _novel_dict(
            id=85633671,
            title="とある魔術の禁書目録",
            author_name="鎌池和馬",
            author_id=12345,
            series_name="とあるシリーズ",
            series_index=1,
        )
        result = tpl.resolve(novel)
        assert result == "鎌池和馬/とあるシリーズ/#1_とある魔術の禁書目録_85633671"

    def test_default_template_without_series_no_residue(self):
        tpl = NovelNamingTemplate(
            "{author_name}/{series_name}/#{series_index}_{title}_{id}"
        )
        novel = _novel_dict(
            id=42,
            title="短編",
            author_name="名無し",
            series_name=None,
            series_index=None,
        )
        result = tpl.resolve(novel)
        assert result == "名無し/短編_42"
        assert "#" not in result


class TestBuildBatchZipWithTemplate:
    """Integration: build_batch_zip uses the template for arcnames."""

    def test_custom_template_is_used(self, tmp_path):
        import zipfile

        # Create a dummy file on disk
        novel_dir = tmp_path / "download" / "0000"
        novel_dir.mkdir(parents=True)
        file_path = novel_dir / "test_title_42.txt"
        file_path.write_text("content", encoding="utf-8")

        novels = [Novel(
            id=42,
            title="Test Title",
            author_name="Author",
            author_id=1,
            like=10,
            view=100,
            text=500,
            series_name="My Series",
            series_index=2,
            create_time="2024-01-15T00:00:00",
            path=str(file_path.with_suffix("")),  # without extension
            has_epub=0,
        )]

        custom = "{id}_{title}"
        buf, titles, missing = build_batch_zip(novels, "txt", custom)

        assert len(titles) == 1
        assert len(missing) == 0

        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert names == ["42_Test Title.txt"]
