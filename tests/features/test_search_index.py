"""Keyword-search index tests — definition, mapping parity, and hits.

Two implementations of the char-gram token stream must stay in lockstep:

* the database function ``copixiv_gram`` (migration 0003) — used by the
  expression index and therefore by every stored row;
* :func:`copixiv.features.novels.search.gram_tokenize` — used to build query
  phrases.

A divergence would silently break every keyword search, so the parity test
below compares the two over a character corpus.  The remaining tests pin the
index definition and the end-to-end behaviour (a keyword hits the right rows
with no explicit re-index call).
"""

import pytest
from sqlalchemy import text

from copixiv.core.services import QuerySpec, parse_search_keyword
from copixiv.db.models import Author, Novel
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.search import gram_tokenize, index_health


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


# A corpus that exercises every branch of the mapping: CJK, kana, latin,
# digits, ASCII and CJK punctuation, whitespace, symbols, emoji, full-width
# forms, and a mix inside one string.
_GRAM_CORPUS = [
    "",
    " ",
    "\t\n\r\x0b\x0c",
    "普通文本",
    "哈利 波特",
    "R-18",
    "催眠の誘い",
    "Harry",
    "hello123",
    "---",
    "...",
    "【前】",
    "“引号”‘单引号’",
    "what's",
    'double"quote',
    "a.b,c;d:e",
    "①②③",
    "½⅓",
    "٣٤٥",
    "Привет",
    "日本語カタカナひらがな",
    "한국어",
    "😀😀",
    "Ｆｕｌｌ　ｗｉｄｔｈ",
    "混合 mixed 123 文本 R-18",
    "龖",
]


class TestGramMappingParity:
    """The Python mirror and the SQL function must agree character by character."""

    # Every code point Python's ``str.isspace()`` accepts.  The database
    # function cannot use ``[[:space:]]`` (POSIX classes follow the database
    # locale — ``C`` here — and are ASCII-only), so the set is spelled out in
    # both implementations and pinned here.
    WHITESPACE_CODE_POINTS = (
        0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20,
        0x85, 0xA0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
        0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F,
        0x205F, 0x3000,
    )

    def test_python_and_sql_gram_agree_on_corpus(self, session_factory):
        with session_factory() as s:
            for sample in _GRAM_CORPUS:
                sql_value = s.execute(
                    text("SELECT copixiv_gram(:t)"), {"t": sample},
                ).scalar()
                assert sql_value == gram_tokenize(sample), (
                    f"gram mapping diverged for {sample!r}: "
                    f"sql={sql_value!r} python={gram_tokenize(sample)!r}"
                )

    def test_whitespace_sets_are_identical(self, session_factory):
        """Every whitespace code point collapses on both sides — and only those."""
        with session_factory() as s:
            for cp in self.WHITESPACE_CODE_POINTS:
                sample = f"a{chr(cp)}b"
                sql_value = s.execute(
                    text("SELECT copixiv_gram(:t)"), {"t": sample},
                ).scalar()
                assert sql_value == "a b", f"SQL kept whitespace {hex(cp)}"
                assert gram_tokenize(sample) == "a b", \
                    f"Python kept whitespace {hex(cp)}"
            # Near misses that must NOT be treated as whitespace: zero-width
            # space, soft hyphen, and an ideographic full stop.
            for cp in (0x200B, 0x00AD, 0x3002):
                sample = f"a{chr(cp)}b"
                sql_value = s.execute(
                    text("SELECT copixiv_gram(:t)"), {"t": sample},
                ).scalar()
                assert sql_value == gram_tokenize(sample), \
                    f"diverged on non-whitespace {hex(cp)}"

    def test_python_and_sql_novel_text_agree(self, session_factory):
        """``copixiv_novel_text`` mirrors the documented field composition."""
        cases = [
            ("标题", "作者", "系列", ["标签A", "标签B"]),
            ("title", None, None, []),
            ("", "author only", "", ["x"]),
            ("R-18 作品", "アリス", "シリーズ 1", ["R-18", "中文"]),
        ]
        with session_factory() as s:
            for title, author, series, tags in cases:
                sql_value = s.execute(
                    text(
                        "SELECT copixiv_novel_text("
                        ":title, :author, :series, :tags)"
                    ),
                    {
                        "title": title, "author": author,
                        "series": series, "tags": tags,
                    },
                ).scalar()
                expected = " ".join(
                    gram_tokenize(part)
                    for part in (title, author, series, " ".join(tags))
                    if part
                )
                assert sql_value == expected, f"novel_text diverged for {cases!r}"


class TestSearchIndexDefinition:
    def test_index_is_an_expression_index_over_novel(self, pg_engine):
        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT pg_get_indexdef(i.indexrelid), c.relname "
                    "FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indrelid "
                    "WHERE i.indexrelid = to_regclass('novel_search_gin')"
                )
            ).first()
        assert row is not None, "novel_search_gin must exist"
        indexdef, table = row
        assert table == "novel", "the search index lives on novel itself"
        assert "USING gin" in indexdef
        assert "copixiv_novel_text" in indexdef

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


class TestKeywordSearchHits:
    """A keyword hits each source column — with no application re-index call."""

    @pytest.fixture
    def seeded(self, session_factory):
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

    async def _search(self, session_factory, keyword: str) -> list[int]:
        with session_factory() as s:
            res = await SQLAlchemyNovelRepository(s).get_novels(
                QuerySpec(
                    conditions=parse_search_keyword(f"keyword:{keyword}"),
                    per_page=50, exclude_blocked_tags=False,
                )
            )
        return [n.id for n in res["novels"]]

    async def test_hits_title(self, seeded):
        assert await self._search(seeded, "催眠") == [1]

    async def test_hits_author_name(self, seeded):
        assert await self._search(seeded, "作者甲") == [1]

    async def test_hits_series_name(self, seeded):
        assert await self._search(seeded, "系列一") == [1]

    async def test_hits_tag(self, seeded):
        assert await self._search(seeded, "幻想") == [1]

    async def test_whitespace_is_and(self, seeded):
        assert await self._search(seeded, "催眠 誘い") == [1]

    async def test_no_hit_keyword_returns_empty(self, seeded):
        assert await self._search(seeded, "不存在的关键词") == []

    async def test_punctuation_is_addressable(self, seeded):
        # "R-18" is stored as R 龖 1 8, so the hyphenated form matches while
        # the collapsed form does not (documented char-gram semantics).
        assert await self._search(seeded, "R-18") == [1]
        assert await self._search(seeded, "R18") == []
