"""Keyword-search tests — index definition, predicate truth, and the old bug.

The search path is **substring matching** (migration 0007): PostgreSQL
generates ``novel.search_lower`` from the row itself, a ``pg_bigm`` GIN index
covers it, and the query side AND-s one ``LIKE`` per whitespace segment.

These tests pin the contract that replaced the char-gram one:

* **round trip** — the repository's result set equals the Python ground truth
  ``{id for id, search_lower in rows if segment in search_lower}`` for a
  keyword corpus (predicate == substring semantics, nothing more);
* **the word-boundary bug stays dead** — a novel whose only matching content
  is the tags ``R-18`` and ``gay`` must NOT match ``R-18G`` (the old
  ``copixiv_gram`` dropped whitespace, so it did — 3,523 false positives in
  the live corpus);
* **wildcards are data** — a tag literally named ``%高中生`` / ``BanG_Dream!``
  matches only itself;
* **case folding is one function** — ``Chinese`` and ``chinese`` return
  identical sets (the predicate lowercases with the database's ``lower()``,
  the same function that produced the column);
* **empty stays "no filter"** — punctuation-only keywords add no predicate.
"""

import pytest
from sqlalchemy import text

from copixiv.core.services import QuerySpec, parse_search_keyword
from copixiv.db.models import Author, Novel
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.search import (
    index_health,
    keyword_condition,
    keyword_params,
    like_predicate,
)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


@pytest.fixture
def seeded(session_factory):
    with session_factory() as s:
        s.add(Author(author_id=1, author_name="作者甲"))
        s.add(Author(author_id=2, author_name="作者乙"))
        s.flush()
        s.add(Novel(
            id=1, title="催眠の誘い", author_id=1, author_name="作者甲",
            series_name="系列一", tags=["R-18", "幻想"],
        ))
        s.add(Novel(
            id=2, title="普通小说", author_id=2, author_name="作者乙",
            tags=["日常"],
        ))
        s.commit()
    return session_factory


async def _search(session, keyword: str) -> list[int]:
    res = await SQLAlchemyNovelRepository(session).get_novels(
        QuerySpec(
            conditions=parse_search_keyword(f"keyword:{keyword}"),
            per_page=50, exclude_blocked_tags=False,
        )
    )
    return sorted(n.id for n in res["novels"])


def _search_lower_rows(session) -> list[tuple[int, str]]:
    return [
        (int(row[0]), row[1] or "")
        for row in session.execute(
            text("SELECT id, search_lower FROM novel")
        ).all()
    ]


def _ground_truth(rows: list[tuple[int, str]], keyword: str) -> set[int]:
    """The documented semantics, computed in Python: every segment must be a
    substring of the lowercased search text."""
    segments = [
        segment for segment in keyword.split()
        if any(ch.isalpha() or ch.isnumeric() for ch in segment)
    ]
    if not segments:
        return {novel_id for novel_id, _ in rows}
    return {
        novel_id
        for novel_id, search_lower in rows
        if all(segment.lower() in search_lower for segment in segments)
    }


class TestKeywordSearchHits:
    """A keyword hits each source column — with no application re-index call."""

    async def test_hits_title(self, seeded):
        with seeded() as s:
            assert await _search(s, "催眠") == [1]

    async def test_hits_author_name(self, seeded):
        with seeded() as s:
            assert await _search(s, "作者甲") == [1]

    async def test_hits_series_name(self, seeded):
        with seeded() as s:
            assert await _search(s, "系列一") == [1]

    async def test_hits_tag(self, seeded):
        with seeded() as s:
            assert await _search(s, "幻想") == [1]

    async def test_whitespace_is_and(self, seeded):
        with seeded() as s:
            assert await _search(s, "催眠 誘い") == [1]
            assert await _search(s, "催眠 幻想") == [1]
            assert await _search(s, "催眠 日常") == []

    async def test_no_hit_keyword_returns_empty(self, seeded):
        with seeded() as s:
            assert await _search(s, "不存在的关键词") == []

    async def test_punctuation_inside_segment_is_substring_data(self, seeded):
        # "R-18" is a literal substring of the tag; the collapsed form is not.
        # (Under the old char-gram design the same two assertions held, but
        # for a different reason: punctuation was a token, not data.)
        with seeded() as s:
            assert await _search(s, "R-18") == [1]
            assert await _search(s, "R18") == []


class TestPredicateTruthRoundTrip:
    """``keyword_condition``/repository results == Python substring truth."""

    KEYWORDS = [
        "催眠",
        "作者甲",
        "系列一",
        "幻想",
        "日常",
        "R-18",
        "R18",
        "作者",
        "催眠 作者甲",
        "R-18 幻想",
        "催眠 不存在",
        "不存在",
        "①",
        "。。",
    ]

    @pytest.fixture
    def corpus(self, session_factory):
        """Rows covering every combination of present/empty search fields."""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="AuthorA"))
            s.add(Author(author_id=2))  # author_name IS NULL
            s.flush()
            s.add(Novel(
                id=1, title="催眠の誘い", author_id=1, author_name="AuthorA",
                series_name="系列一", tags=["R-18", "幻想"],
            ))
            s.add(Novel(
                id=2, title="普通小说", author_id=2, author_name=None,
                tags=["日常", "①"],
            ))
            s.add(Novel(
                id=3, title="MixedCASE Title", author_id=1,
                author_name="AuthorA", series_name="Series X",
                tags=["Chinese", "BanG_Dream!"],
            ))
            s.commit()
        return session_factory

    async def test_every_keyword_matches_the_python_truth(self, corpus):
        with corpus() as s:
            rows = _search_lower_rows(s)
            assert rows, "corpus must be seeded"
            for keyword in self.KEYWORDS:
                expected = _ground_truth(rows, keyword)
                actual = set(await _search(s, keyword))
                assert actual == expected, (
                    f"keyword {keyword!r}: repo={sorted(actual)} "
                    f"truth={sorted(expected)}"
                )

    async def test_keyword_condition_reaches_the_same_rows(self, corpus):
        """The predicate the repository binds is the same one a raw query uses."""
        with corpus() as s:
            rows = _search_lower_rows(s)
            for keyword in ("催眠", "R-18", "催眠 作者A", "不存在"):
                predicate = like_predicate(keyword)
                assert predicate is not None
                hit_ids = s.execute(
                    text(f"SELECT id FROM novel WHERE {predicate}").bindparams(
                        **keyword_params(keyword)
                    )
                ).scalars().all()
                assert set(hit_ids) == _ground_truth(rows, keyword)


class TestWordBoundaryRegression:
    """The pre-0007 false positive must be impossible.

    ``copixiv_gram`` dropped whitespace, so ``R-18`` and ``gay`` became
    adjacent tokens and ``R-18G`` matched a novel whose original text contains
    no such substring (3,523 such rows in the live corpus).
    """

    @pytest.fixture
    def two_rows(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            # The exact live shape: two *separate* tags.
            s.add(Novel(
                id=1, title="标签拼接假阳性", author_id=1,
                tags=["R-18", "gay"],
            ))
            # The genuine article: the substring really is present.
            s.add(Novel(
                id=2, title="真正的 R-18G", author_id=1, tags=["R-18G"],
            ))
            s.commit()
        return session_factory

    async def test_r18g_does_not_match_r18_plus_gay(self, two_rows):
        with two_rows() as s:
            hits = await _search(s, "R-18G")
        assert hits == [2], (
            "R-18G must match only the row that really contains the substring; "
            f"got {hits}"
        )

    async def test_result_set_is_exactly_the_substring_truth(self, two_rows):
        with two_rows() as s:
            rows = _search_lower_rows(s)
            hits = set(await _search(s, "R-18G"))
            assert hits == {
                novel_id for novel_id, value in rows if "r-18g" in value
            }
            assert 1 not in hits

    async def test_multi_segment_keyword_never_crosses_the_boundary(self, two_rows):
        """``R-18 gay`` is the honest way to ask for the two-tag novel."""
        with two_rows() as s:
            assert await _search(s, "R-18 gay") == [1]
            assert await _search(s, "真正的 拼接") == []


class TestWildcardEscaping:
    """``%``/``_``/``\\`` in a keyword are data, never wildcards."""

    @pytest.fixture
    def wildcards(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="literal", author_id=1, tags=["%高中生"]))
            s.add(Novel(id=2, title="decoy", author_id=1, tags=["X高中生"]))
            s.add(Novel(id=3, title="dream", author_id=1, tags=["BanG_Dream!"]))
            s.add(Novel(id=4, title="nodream", author_id=1, tags=["BanGDream!"]))
            s.add(Novel(id=5, title="backslash", author_id=1, tags=["a\\b"]))
            s.commit()
        return session_factory

    async def test_percent_is_literal(self, wildcards):
        # Unescaped, ``%高中生`` would be ``LIKE '%%高中生%'`` and match the
        # decoy ``X高中生`` too (the live tag-suggestion path has this bug).
        with wildcards() as s:
            assert await _search(s, "%高中生") == [1]

    async def test_underscore_is_literal(self, wildcards):
        # Unescaped, ``_`` matches any single character.
        with wildcards() as s:
            assert await _search(s, "BanG_Dream!") == [3]

    async def test_backslash_is_literal(self, wildcards):
        with wildcards() as s:
            assert await _search(s, "a\\b") == [5]

    async def test_escaped_keywords_match_the_truth(self, wildcards):
        with wildcards() as s:
            rows = _search_lower_rows(s)
            for keyword in ("%高中生", "BanG_Dream!", "a\\b"):
                assert set(await _search(s, keyword)) == _ground_truth(rows, keyword)


class TestCaseFolding:
    """The pattern is lowercased with the database's ``lower()`` on both sides."""

    @pytest.fixture
    def mixed_case(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="Chinese Story", author_id=1, tags=[]))
            s.add(Novel(id=2, title="chinese story", author_id=1, tags=[]))
            s.add(Novel(id=3, title="CHINESE STORY", author_id=1, tags=[]))
            s.add(Novel(id=4, title="无关", author_id=1, tags=[]))
            s.commit()
        return session_factory

    async def test_upper_and_lower_keywords_return_identical_sets(self, mixed_case):
        with mixed_case() as s:
            upper = await _search(s, "Chinese")
            lower = await _search(s, "chinese")
        assert upper == lower == [1, 2, 3]

    async def test_column_and_predicate_agree_on_folding(self, mixed_case):
        """Whatever ``lower()`` does, both sides do the same thing."""
        with mixed_case() as s:
            rows = _search_lower_rows(s)
            for keyword in ("Chinese", "chinese", "CHINESE STORY"):
                assert set(await _search(s, keyword)) == _ground_truth(rows, keyword)


class TestNoFilterContract:
    """A keyword that collapses to nothing filters nothing."""

    async def test_predicate_is_none(self):
        assert like_predicate("") is None
        assert like_predicate("   ") is None
        assert like_predicate("---") is None
        assert like_predicate("（）") is None
        assert keyword_condition("---") is None

    async def test_repository_returns_everything(self, seeded):
        with seeded() as s:
            assert await _search(s, "---") == [1, 2]
            assert await _search(s, "（）") == [1, 2]
            # ... while a real (but absent) keyword really does filter.
            assert await _search(s, "不存在") == []


class TestSearchIndexDefinition:
    def test_index_is_a_bigm_gin_index_over_search_lower(self, pg_engine):
        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT pg_get_indexdef(i.indexrelid), c.relname "
                    "FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indrelid "
                    "WHERE i.indexrelid = to_regclass('novel_search_bigm')"
                )
            ).first()
        assert row is not None, "novel_search_bigm must exist"
        indexdef, table = row
        assert table == "novel", "the search index lives on novel itself"
        assert "USING gin" in indexdef
        assert "search_lower" in indexdef
        assert "gin_bigm_ops" in indexdef

    def test_derived_search_table_is_gone(self, pg_engine):
        with pg_engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('novel_search')")
            ).scalar() is None

    def test_index_health_reports_valid(self, session_factory):
        with session_factory() as s:
            health = index_health(s)
        assert health["index_exists"] is True
        assert health["is_valid"] is True
        assert health["is_healthy"] is True
