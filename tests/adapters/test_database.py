"""Database integrity / search-index / index tests for the PostgreSQL foundation.

Post-migration rewrite of the SQLite-era module: the ``novel_tag`` /
``favourite`` join tables are gone (tags live in ``novel.tags text[]``,
``is_favourite`` is a ``novel`` column), keyword search is an expression GIN
index over ``novel`` (no FTS5 virtual table, no derived search table), and the
PRAGMA kitchen-sink is replaced by PostgreSQL-native checks.

Search behaviour (index definition, char-gram mapping, freshness) lives in
``tests/features/test_search_index.py`` and
``tests/regression/test_search_index_freshness.py``.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import (
    Author, FailedNovel, Novel, Tag, TagAlias,
)
from copixiv.features.novels.search import gram_tokenize


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class TestModels:
    def test_create_author(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="test"))
            s.commit()
            assert s.get(Author, 1).author_name == "test"

    def test_create_novel(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=10, author_name="auth"))
            s.flush()
            s.add(Novel(id=100, title="Novel", author_id=10, path="/tmp/test.txt",
                        tags=["R-18"], is_favourite=True))
            s.commit()
            row = s.get(Novel, 100)
            assert row.title == "Novel"
            assert row.author_id == 10
            assert row.tags == ["R-18"]
            assert row.is_favourite is True

    def test_is_favourite_bool_column(self, session_factory):
        """favourite is a boolean novel column now, not a join table."""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt",
                        is_favourite=False))
            s.commit()
            s.get(Novel, 1).is_favourite = True
            s.commit()
            assert s.get(Novel, 1).is_favourite is True


class TestForeignKeyIntegrity:
    def test_novel_requires_valid_author(self, session_factory):
        """Inserting a novel with non-existent author_id should fail."""
        with session_factory() as s:
            s.add(Novel(id=1, title="Orphan", author_id=999, path="/tmp/orphan.txt"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

    def test_tag_alias_requires_valid_source_tag(self, session_factory):
        with session_factory() as s:
            s.add(Tag(name="valid_tag", reference_count=0))
            s.flush()
            s.add(TagAlias(source=1, target=999))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

    def test_tag_alias_requires_valid_target_tag(self, session_factory):
        with session_factory() as s:
            s.add(Tag(name="valid_tag", reference_count=0))
            s.flush()
            s.add(TagAlias(source=999, target=1))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()


class TestCascadeDelete:
    async def test_delete_novel_cleans_failed_novel_ledger(
        self, session_factory,
    ):
        """Deleting a novel through the repository drops its failure-ledger row
        (explicit cleanup — the ledger has no FK by design).  The search index
        needs no cleanup: it is an expression index over ``novel``."""
        from copixiv.features.novels.repo import SQLAlchemyNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
            s.add(FailedNovel(
                novel_id=1, failure_type="download", error_message="e",
                failed_times=1, last_failed_at=datetime.now(timezone.utc),
            ))
            s.commit()

            await SQLAlchemyNovelRepository(s).delete(1)
            s.commit()

            assert s.get(Novel, 1) is None
            assert s.get(FailedNovel, 1) is None


class TestIndexesExist:
    EXPECTED_INDEXES = {
        "ix_novel_like_text_id",
        "ix_novel_like_id",
        "ix_novel_shuffle_id",
        "ix_novel_shuffle_like_text",
        "ix_novel_author_id",
        "ix_novel_series_id",
        "ix_novel_author_like",
        "ix_novel_series_like",
        "ix_novel_create_time",
        "ix_novel_tags_gin",
        "ix_novel_favourite",
        "ix_author_special_follow",
        "ix_author_last_update",
        "ix_series_author_id",
        "ix_tag_alias_target",
        "ix_search_history_type_timestamp",
        "ux_task_history_running",
        "novel_search_gin",
        "ix_failed_novel_last_failed_at",
    }

    def test_all_expected_indexes_exist(self, pg_engine):
        with pg_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
            ).scalars().all()
        actual = set(rows)
        missing = self.EXPECTED_INDEXES - actual
        assert not missing, f"Missing indexes: {missing}"


class TestGramTokenize:
    """Character-unigram tokeniser — the single source of truth (R1 guard)."""

    def test_empty_string(self):
        assert gram_tokenize("") == ""

    def test_pure_whitespace(self):
        assert gram_tokenize("   ") == ""
        assert gram_tokenize(" \t\n  ") == ""

    def test_pure_punctuation_maps_to_placeholder(self):
        assert gram_tokenize("---") == "龖 龖 龖"
        assert gram_tokenize("...") == "龖 龖 龖"

    def test_cjk_chars_kept(self):
        assert gram_tokenize("普通文本") == "普 通 文 本"

    def test_latin_alphanumeric_kept_case_preserved(self):
        assert gram_tokenize("Harry") == "H a r r y"
        assert gram_tokenize("hello123") == "h e l l o 1 2 3"

    def test_punctuation_maps_to_placeholder(self):
        assert gram_tokenize("R-18") == "R 龖 1 8"

    def test_whitespace_inside_text_is_skipped(self):
        assert gram_tokenize("哈利 波特") == "哈 利 波 特"

    def test_non_ascii_symbols_are_kept(self):
        # The simple tokeniser recognises every non-ASCII character, so CJK
        # punctuation and emoji stay addressable as themselves (only ASCII
        # punctuation collapses to the placeholder).
        assert gram_tokenize("😀😀") == "😀 😀"
        assert gram_tokenize("【前】") == "【 前 】"
        assert gram_tokenize("。，") == "。 ，"

    def test_unicode_whitespace_is_dropped(self):
        # Whitespace is dropped on both sides (index collapse == query split).
        assert gram_tokenize("哈利\u3000波特") == "哈 利 波 特"
        assert gram_tokenize("a\u00a0b") == "a b"


class TestConnectionPoolConfig:
    def test_lock_timeout_set(self):
        """The application engine sets a lock_timeout so a stuck lock fails fast."""
        from copixiv.db.engine import create_database_engine

        engine = create_database_engine(
            "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test"
        )
        try:
            with engine.connect() as conn:
                val = conn.execute(text("SHOW lock_timeout")).scalar()
                # lock_timeout default is '0' (disabled); the engine sets 60s.
                assert val is not None and val.strip() != "0"
        finally:
            engine.dispose()
