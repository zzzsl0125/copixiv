"""Tests for the char-gram keyword → tsquery construction.

The function under test is :func:`copixiv.features.novels.search.build_tsquery`
— a pure function (string in, string out) that turns a user keyword into the
``tsquery`` phrase list bound to ``to_tsquery('simple', ...)``.

Char-gram semantics under test (docs/TRIGRAM_FEASIBILITY.md §2-3):
  * whitespace = AND: a keyword is split into segments, one char-gram phrase each;
  * a segment without whitespace matches an exact contiguous substring
    (``哈利波特`` → the phrase ``'哈 利 波 特'``);
  * pure-punctuation segments are dropped (a keyword that collapses to
    nothing filters nothing, preserving the empty = no-filter contract);
  * every non-alphanumeric character (including ``"`` and ``'``) maps to the
    placeholder ``龖``, so the emitted phrase never contains a quote
    character and the tsquery language is never a syntax risk.

The Python mapping (``gram_tokenize``) mirrors the database function
``copixiv_gram``; ``tests/features/test_search_index.py`` asserts the two
agree character by character.
"""

from copixiv.features.novels.search import build_tsquery, gram_tokenize

build = build_tsquery


class TestBuildTsquery:
    """Table-driven tests on the pure query builder (char-gram contract)."""

    def test_empty_and_blank_input(self):
        # Empty = no keyword condition is emitted by the caller (contract).
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
        # One segment → one quoted phrase (adjacency match).
        assert build("扶她女校") == "'扶 她 女 校'"
        assert build("哈利波特") == "'哈 利 波 特'"

    def test_whitespace_is_and(self):
        assert build("哈利 波特") == "'哈 利' & '波 特'"
        assert build("扶她 女校") == "'扶 她' & '女 校'"

    def test_latin_single_word_is_char_phrase(self):
        assert build("Harry") == "'H a r r y'"
        assert build("vocaloid オリジナル") == (
            "'v o c a l o i d' & 'オ リ ジ ナ ル'"
        )

    def test_reserved_words_are_plain_phrases_not_operators(self):
        # AND/OR/NOT/NEAR become plain character phrases inside quotes; no
        # reserved-word handling is needed.
        assert build("AND") == "'A N D'"
        assert build("OR") == "'O R'"
        assert build("NOT") == "'N O T'"
        assert build("NEAR") == "'N E A R'"
        assert build("and") == "'a n d'"

    def test_reserved_word_kept_in_mixed_query(self):
        assert build("and harry") == "'a n d' & 'h a r r y'"

    def test_apostrophe_maps_to_placeholder(self):
        # gram_tokenize maps ' → 龖, so the phrase contains no raw quote char.
        assert build("what's") == "'w h a t 龖 s'"
        assert build("don't stop") == "'d o n 龖 t' & 's t o p'"

    def test_punctuation_inside_word_kept(self):
        # ASCII punctuation collapses to the placeholder (so "R18" cannot
        # match "R-18"); non-ASCII punctuation is a token in its own right.
        assert build("R-18") == "'R 龖 1 8'"
        assert build("one. two") == "'o n e 龖' & 't w o'"
        assert build("【前") == "'【 前'"

    def test_no_quote_injected_from_input(self):
        # The only single-quote characters are the phrase delimiters: a quote
        # in the keyword maps to the placeholder, never to the query language.
        result = build("what's \"quoted\"")
        assert result == "'w h a t 龖 s' & '龖 q u o t e d 龖'"

    def test_gram_tokenize_is_the_phrase_body(self):
        # The phrase body is exactly gram_tokenize(segment).
        for keyword in ("哈利波特", "R-18", "what's", "Harry"):
            assert f"'{gram_tokenize(keyword)}'" == build(keyword)
