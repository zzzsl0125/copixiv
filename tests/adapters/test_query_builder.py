"""Tests for the bigram keyword → ``LIKE`` predicate construction.

The functions under test live in :mod:`copixiv.features.novels.search` and are
pure (strings in, strings/dicts out):

* :func:`normalize_segments` — whitespace splitting + noise-segment dropping;
* :func:`escape_like` — ``\\`` / ``%`` / ``_`` escaping for ``ESCAPE '\\'``;
* :func:`keyword_params` — one ``%…%`` bind value per segment;
* :func:`like_predicate` — ``search_lower LIKE lower(:pN) ESCAPE '\\'`` per
  segment, ``AND``-ed, or ``None`` when nothing is searchable;
* :func:`keyword_condition` — the SQLAlchemy ``text()`` wrapper the repository
  binds.

Semantics under test (``docs/perf/PG_BIGM_FEASIBILITY.md`` §2):

  * whitespace = AND: ``哈利 波特`` matches novels containing both substrings;
  * every segment is a **substring** match (``R-18`` cannot match ``R18``, and
    ``R-18G`` cannot match an ``R-18``/``gay`` tag pair);
  * pure-punctuation segments are dropped (a keyword that collapses to nothing
    filters nothing — the empty = no-filter contract);
  * ``%`` / ``_`` / ``\\`` in the keyword are data, not wildcards;
  * lowercasing happens in SQL (``lower(:pN)``), not in Python, so the pattern
    and the generated ``search_lower`` column use the same ``lower()`` —
    ``tests/features/test_search_index.py`` pins the end-to-end equivalence.
"""

from sqlalchemy.dialects import postgresql

from copixiv.features.novels.search import (
    escape_like,
    keyword_condition,
    keyword_params,
    like_predicate,
    normalize_segments,
)

PREDICATE = r"search_lower LIKE lower(:p0) ESCAPE '\'"


class TestNormalizeSegments:
    """Whitespace splitting + "no letter/digit = no meaning" dropping."""

    def test_empty_and_blank(self):
        assert normalize_segments("") == []
        assert normalize_segments("   ") == []
        assert normalize_segments("\t\n") == []

    def test_pure_punctuation_dropped(self):
        assert normalize_segments("---") == []
        assert normalize_segments("...") == []
        assert normalize_segments("!!!") == []
        assert normalize_segments("--- ...") == []

    def test_whitespace_splits_into_segments(self):
        assert normalize_segments("哈利 波特") == ["哈利", "波特"]
        assert normalize_segments("R-18 中文") == ["R-18", "中文"]

    def test_unicode_whitespace_splits(self):
        # Python's ``str.split()`` set — the same set the old query side used.
        assert normalize_segments("哈利\u3000波特") == ["哈利", "波特"]
        assert normalize_segments("a\u00a0b") == ["a", "b"]

    def test_noise_segments_are_dropped_but_kept_neighbours_stay(self):
        assert normalize_segments("哈利 --- 波特") == ["哈利", "波特"]
        # A segment only needs *one* letter/digit to count.
        assert normalize_segments("R-18") == ["R-18"]
        assert normalize_segments("①") == ["①"]


class TestEscapeLike:
    def test_plain_value_untouched(self):
        assert escape_like("哈利波特") == "哈利波特"
        assert escape_like("R-18") == "R-18"

    def test_percent_underscore_backslash_escaped(self):
        assert escape_like("a%b") == r"a\%b"
        assert escape_like("x_y") == r"x\_y"
        assert escape_like("a\\b") == r"a\\b"

    def test_backslash_is_escaped_first(self):
        # ``\%`` must not become ``\\%`` (which would mean "literal backslash
        # then wildcard").
        assert escape_like("\\%") == r"\\\%"

    def test_real_world_tag_names(self):
        assert escape_like("%高中生") == r"\%高中生"
        assert escape_like("BanG_Dream!") == r"BanG\_Dream!"


class TestKeywordParams:
    def test_one_pattern_per_segment(self):
        assert keyword_params("哈利 波特") == {"p0": "%哈利%", "p1": "%波特%"}
        assert keyword_params("R-18") == {"p0": "%R-18%"}

    def test_no_segments_no_params(self):
        assert keyword_params("") == {}
        assert keyword_params("---") == {}

    def test_case_is_left_to_sql_lower(self):
        # The predicate lowercases with the database's ``lower()`` so both
        # sides call the same function (C locale folds ASCII only); the bind
        # value therefore keeps the user's casing.
        assert keyword_params("Chinese") == {"p0": "%Chinese%"}
        assert "lower(:p0)" in PREDICATE

    def test_wildcards_are_escaped_data(self):
        assert keyword_params("%高中生") == {"p0": r"%\%高中生%"}
        assert keyword_params("BanG_Dream!") == {"p0": r"%BanG\_Dream!%"}

    def test_quotes_are_plain_data(self):
        # No query language is involved anymore: quotes need no escaping.
        assert keyword_params("what's") == {"p0": "%what's%"}
        # ... and whitespace still splits (the quote is just another character).
        assert keyword_params('say "hi"') == {"p0": "%say%", "p1": '%"hi"%'}


class TestLikePredicate:
    def test_no_searchable_segment_returns_none(self):
        assert like_predicate("") is None
        assert like_predicate("   ") is None
        assert like_predicate("---") is None
        assert like_predicate("--- ...") is None

    def test_single_segment(self):
        assert like_predicate("哈利") == PREDICATE

    def test_multiple_segments_are_and_ed(self):
        assert like_predicate("哈利 波特") == (
            r"search_lower LIKE lower(:p0) ESCAPE '\' AND "
            r"search_lower LIKE lower(:p1) ESCAPE '\'"
        )

    def test_noise_segments_do_not_consume_placeholders(self):
        assert like_predicate("哈利 --- 波特") == (
            r"search_lower LIKE lower(:p0) ESCAPE '\' AND "
            r"search_lower LIKE lower(:p1) ESCAPE '\'"
        )

    def test_matches_the_generated_column_not_the_expression(self):
        # The pg_bigm GIN index is on ``novel.search_lower``; the predicate
        # must reference exactly that column (unqualified, as the builder
        # selects from ``novel`` with no alias) to be indexable.
        predicate = like_predicate("催眠")
        assert predicate is not None
        assert predicate.startswith("search_lower LIKE ")
        assert "to_tsvector" not in predicate


class TestKeywordCondition:
    def test_none_when_nothing_is_searchable(self):
        assert keyword_condition("") is None
        assert keyword_condition("   ") is None
        assert keyword_condition("（）") is None
        assert keyword_condition("..") is None

    def test_condition_carries_the_segment_params(self):
        condition = keyword_condition("哈利 波特")
        assert condition is not None
        compiled = condition.compile(dialect=postgresql.dialect())
        assert compiled.params == {"p0": "%哈利%", "p1": "%波特%"}
        sql = str(compiled)
        # ``text()`` binds render with an explicit cast (SQLAlchemy 2.0), so
        # match the prefix rather than the exact placeholder rendering.
        assert "search_lower LIKE lower(%(p0)s" in sql
        assert "search_lower LIKE lower(%(p1)s" in sql
        assert sql.count("ESCAPE") == 2

    def test_reserved_words_are_plain_data(self):
        # The tsquery language (AND/OR/NOT/NEAR) is gone with the old design:
        # these are ordinary LIKE patterns now.
        for word in ("AND", "OR", "NOT", "NEAR"):
            assert keyword_params(word) == {"p0": f"%{word}%"}
