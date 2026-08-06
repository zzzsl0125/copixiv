"""Tests for FTS query-string construction in query_builder.

The core function under test is ``BaseQueryBuilder._build_fts_query_string``
— a pure function (string in, string out), but the output is embedded
verbatim into a SQL string literal::

    MATCH '{fts_query}'

so it must be safe both as SQL and as an FTS5 MATCH expression.  The
integration tests below execute the built query against a real in-memory
FTS5 table to prove no syntax error can reach the database layer.
"""

import sqlite3

import pytest

from copixiv.infrastructure.repositories.query_builder import BaseQueryBuilder

build = BaseQueryBuilder._build_fts_query_string


class TestBuildFtsQueryString:
    """Table-driven tests on the pure query builder."""

    def test_empty_and_blank_input(self):
        assert build("") == ""
        assert build("   ") == ""

    def test_pure_punctuation_dropped(self):
        # Pure-punctuation tokens are invalid in FTS5 MATCH queries.
        assert build("---") == ""
        assert build("...") == ""

    def test_fts5_reserved_words_dropped(self):
        # Bare AND/OR/NOT/NEAR would be parsed as operators and produce
        # an FTS5 syntax error (e.g. "AND" alone is a syntax error).
        for kw in ("AND", "OR", "NOT", "NEAR"):
            assert build(kw) == ""
            assert build(kw.lower()) == ""

    def test_reserved_word_removed_from_mixed_query(self):
        assert build("and harry") == "harry"

    def test_single_quotes_stripped(self):
        # A bare quote would start an unterminated FTS5 string literal;
        # current behaviour strips them.
        assert build("what's") == "what AND s"
        assert build("don't stop") == "don AND t AND stop"

    def test_tokens_joined_with_and(self):
        assert build("Harry Potter") == "Harry AND Potter"
        assert build("哈利 波特") == "哈利 AND 波特"

    def test_multiple_language_tokens(self):
        assert build("vocaloid オリジナル") == (
            "vocaloid AND オ AND リ AND ジ AND ナ AND ル"
        )


class TestFtsQueryExecutes:
    """Integration: every built query must execute without syntax errors."""

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE novel_fts USING fts5(title)")
        except sqlite3.OperationalError:
            pytest.skip("SQLite build lacks FTS5 support")
        conn.execute(
            "INSERT INTO novel_fts VALUES ('Harry Potter 哈利波特')"
        )
        return conn

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
    ])
    def test_no_syntax_error(self, conn, keyword):
        query = build(keyword)
        if not query:
            return  # empty query → no MATCH clause is emitted by the caller
        # Same embedding style as _where_fts_filter:
        conn.execute(
            f"SELECT rowid FROM novel_fts WHERE novel_fts MATCH '{query}'"
        )
