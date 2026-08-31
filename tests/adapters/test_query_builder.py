"""Tests for char-gram FTS query-string construction in query_builder.

The core function under test is ``BaseQueryBuilder._build_fts_query_string``
— a pure function (string in, string out).  Under PostgreSQL the output is
*unquoted* char-gram text joined with ``&`` (AND); the phase that wraps each
gram phrase in single quotes for ``to_tsquery('simple', ...)`` lives in
:func:`copixiv.features.novels.repo.fts_query_to_pg`.  Both are pure and are
tested here directly.

Char-gram semantics under test (docs/TRIGRAM_FEASIBILITY.md §2-3, adapted to
the PG ``to_tsquery`` phrase form):
  * whitespace = AND: a keyword is split into segments, one char-gram phrase each;
  * a segment without whitespace matches an exact contiguous substring
    (``哈利波特`` → ``哈 利 波 特``);
  * pure-punctuation segments are dropped (a keyword that collapses to
    nothing filters nothing, preserving the empty = no-MATCH contract);
  * every non-alphanumeric character (including ``"`` and ``'``) maps to the
    placeholder ``龖``, so the emitted phrase never contains a quote
    character and the tsquery language is never a syntax risk.
"""

import pytest

from copixiv.features.novels.repo import BaseQueryBuilder, fts_query_to_pg

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
        # No double-quote form anymore — the output is bare char-gram text.
        assert build("扶她女校") == "扶 她 女 校"
        assert build("哈利波特") == "哈 利 波 特"

    def test_whitespace_is_and(self):
        assert build("哈利 波特") == "哈 利 & 波 特"
        assert build("扶她 女校") == "扶 她 & 女 校"

    def test_latin_single_word_is_char_phrase(self):
        assert build("Harry") == "H a r r y"
        assert build("vocaloid オリジナル") == (
            "v o c a l o i d & オ リ ジ ナ ル"
        )

    def test_reserved_words_are_plain_phrases_not_operators(self):
        # AND/OR/NOT/NEAR become plain character phrases; no reserved-word
        # handling is needed because the phrase is quoted by fts_query_to_pg.
        assert build("AND") == "A N D"
        assert build("OR") == "O R"
        assert build("NOT") == "N O T"
        assert build("NEAR") == "N E A R"
        assert build("and") == "a n d"

    def test_reserved_word_kept_in_mixed_query(self):
        assert build("and harry") == "a n d & h a r r y"

    def test_apostrophe_maps_to_placeholder(self):
        # gram_tokenize maps ' → 龖, so the phrase contains no raw quote char.
        assert build("what's") == "w h a t 龖 s"
        assert build("don't stop") == "d o n 龖 t & s t o p"

    def test_punctuation_inside_word_kept(self):
        assert build("R-18") == "R 龖 1 8"
        assert build("one. two") == "o n e 龖 & t w o"
        assert build("【前") == "龖 前"


class TestFtsQueryToPg:
    """The PG phrase-wrapping phase: bare gram text → single-quote tsquery."""

    def test_empty(self):
        assert fts_query_to_pg("") == ""

    def test_single_phrase(self):
        assert fts_query_to_pg("哈 利 波 特") == "'哈 利 波 特'"

    def test_and_phrases(self):
        assert fts_query_to_pg("哈 利 & 波 特") == "'哈 利' & '波 特'"

    def test_punctuation_phrase(self):
        assert fts_query_to_pg("R 龖 1 8") == "'R 龖 1 8'"

    def test_roundtrip_gram_tokenize(self):
        # The full keyword→gram→tsquery phrase pipeline.
        kw = "哈利 波特"
        assert fts_query_to_pg(build(kw)) == "'哈 利' & '波 特'"

    def test_no_quote_injected_from_input(self):
        # A keyword containing a quote char must still yield a valid tsquery
        # phrase (the quote maps to the placeholder, never reaches the query).
        # The only single-quote chars are the wrapping phrase delimiters.
        assert fts_query_to_pg(build("what's")) == "'w h a t 龖 s'"
