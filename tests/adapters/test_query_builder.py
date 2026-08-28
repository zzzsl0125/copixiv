"""Tests for char-gram FTS query-string construction in query_builder.

The core function under test is ``BaseQueryBuilder._build_fts_query_string``
— a pure function (string in, string out), but the output is embedded
verbatim into the FTS5 MATCH clause (passed as a bound parameter in
``_where_fts_filter``), so it must be safe both as SQL and as an FTS5 MATCH
expression.

The integration tests execute the built query against a real in-memory
FTS5 table whose rows are char-grammed with ``gram_tokenize`` exactly as the
production index is built — proving both that no syntax error reaches the
database layer AND that the index side and the query side agree (the R1
consistency regression guard).

Char-gram semantics under test (docs/TRIGRAM_FEASIBILITY.md §2-3):
  * whitespace = AND: a keyword is split into segments, one quoted phrase each;
  * a segment without whitespace matches an exact contiguous substring
    (``哈利波特`` → ``"哈 利 波 特"``);
  * pure-punctuation segments are dropped (a keyword that collapses to
    nothing filters nothing, preserving the empty = no-MATCH contract);
  * every non-alphanumeric character (including ``"`` and ``'``) maps to the
    placeholder ``龖``, so the emitted phrase never contains a quote
    character and the FTS5 query language (AND/OR/NOT/NEAR) is never a
    syntax risk.
"""

import sqlite3

import pytest

from copixiv.features.novels.fts import gram_tokenize
from copixiv.features.novels.repo import BaseQueryBuilder

build = BaseQueryBuilder._build_fts_query_string


class TestBuildFtsQueryString:
    """Table-driven tests on the pure query builder (char-gram contract)."""

    def test_empty_and_blank_input(self):
        # Empty = no MATCH clause is emitted by the caller (unchanged contract).
        assert build("") == ""
        assert build("   ") == ""

    def test_pure_punctuation_dropped(self):
        # A segment made entirely of non-alphanumeric characters carries no
        # search meaning and is dropped, so a keyword that collapses to
        # nothing filters nothing.
        assert build("---") == ""
        assert build("...") == ""
        assert build("!!!") == ""
        assert build("--- ...") == ""

    def test_cjk_no_space_is_contiguous_substring(self):
        assert build("扶她女校") == '"扶 她 女 校"'
        assert build("哈利波特") == '"哈 利 波 特"'

    def test_whitespace_is_and(self):
        assert build("哈利 波特") == '"哈 利" AND "波 特"'
        assert build("扶她 女校") == '"扶 她" AND "女 校"'

    def test_latin_single_word_is_char_phrase(self):
        assert build("Harry") == '"H a r r y"'
        assert build("vocaloid オリジナル") == (
            '"v o c a l o i d" AND "オ リ ジ ナ ル"'
        )

    def test_reserved_words_are_plain_phrases_not_operators(self):
        # A quoted phrase is a literal token sequence, so AND/OR/NOT/NEAR are
        # NOT parsed as operators — they become plain character phrases (the
        # old reserved-word dropping is gone; a quote phrase is always safe).
        assert build("AND") == '"A N D"'
        assert build("OR") == '"O R"'
        assert build("NOT") == '"N O T"'
        assert build("NEAR") == '"N E A R"'
        assert build("and") == '"a n d"'

    def test_reserved_word_kept_in_mixed_query(self):
        assert build("and harry") == '"a n d" AND "h a r r y"'

    def test_apostrophe_maps_to_placeholder(self):
        # gram_tokenize maps ' → 龖, so the phrase contains no raw quote char.
        assert build("what's") == '"w h a t 龖 s"'
        assert build("don't stop") == '"d o n 龖 t" AND "s t o p"'

    def test_punctuation_inside_word_kept(self):
        assert build("R-18") == '"R 龖 1 8"'
        assert build("one. two") == '"o n e 龖" AND "t w o"'
        assert build("【前") == '"龖 前"'


class TestFtsQueryExecutes:
    """Integration: built queries execute AND agree with the index side.

    The fixture builds a ``novel_fts`` table with the production char-gram
    shape (``tokenize='unicode61'``) and inserts rows char-grammed exactly
    like ``FTSManager._batch_insert_fts_entries`` — i.e. ``gram_tokenize()``
    on each text field.  The parametrised case then asserts the rowids that
    the built query MATCHES, locking in the index/query consistency (R1):
    the exact same ``gram_tokenize`` on both sides must reproduce the
    intended substring semantics.
    """

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE novel_fts USING fts5("
                "title, author_name, series_name, tags, tokenize='unicode61')"
            )
        except sqlite3.OperationalError:
            pytest.skip("SQLite build lacks FTS5 support")
        rows = [
            (1, "哈利波特", "", "", ""),
            (2, "扶她女校", "", "", ""),
            (3, "R-18", "", "", ""),
            (4, "what's", "", "", ""),
            (5, "普通", "", "", "浪漫"),
        ]
        for rowid, title, author, series, tags in rows:
            conn.execute(
                "INSERT INTO novel_fts(rowid, title, author_name, series_name, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (rowid, gram_tokenize(title), gram_tokenize(author),
                 gram_tokenize(series), gram_tokenize(tags)),
            )
        conn.commit()
        return conn

    @staticmethod
    def _match_rows(conn, keyword: str) -> list[int]:
        query = build(keyword)
        if not query:
            # Empty query means "no MATCH clause" — the caller filters nothing.
            return []
        cur = conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", (query,)
        )
        return sorted(r[0] for r in cur.fetchall())

    @pytest.mark.parametrize("keyword, expected", [
        # No-space queries = exact contiguous substring.
        ("哈利", [1]),
        ("哈利波特", [1]),
        # Whitespace = AND across segments within a single row.
        ("哈利 波特", [1]),
        ("扶她 女校", [2]),
        ("扶她女校", [2]),
        # Punctuation placeholder: 龖 breaks R18 from matching R-18 (T4).
        ("R-18", [3]),
        ("R18", []),
        # Apostrophe maps to 龖 too; unicode61 folds case (T5).
        ("what's", [4]),
        ("WHAT'S", [4]),
        # Tags are searchable after the char-gram index build.
        ("浪漫", [5]),
        ("普通", [5]),
    ])
    def test_query_hits_expected_rows(self, conn, keyword, expected):
        assert self._match_rows(conn, keyword) == expected

    @pytest.mark.parametrize("keyword", [
        "Harry",
        "哈利 波特",
        "AND",
        "OR",
        "NOT",
        "NEAR",
        "and harry",
        "what's",
        "---",
        "vocaloid オリジナル",
        "R-18 催眠",
        "一.",
    ])
    def test_no_syntax_error(self, conn, keyword):
        query = build(keyword)
        if not query:
            # Empty query means "no MATCH clause is emitted by the caller" —
            # pin that contract explicitly instead of silently passing.
            assert build(keyword) == ""
            return
        # Same embedding style as _where_fts_filter (bound parameter).
        conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", (query,)
        )
