"""Database integrity / FTS / index tests for the PostgreSQL foundation.

Post-migration rewrite of the SQLite-era module: the ``novel_tag`` /
``favourite`` join tables are gone (tags live in ``novel.tags text[]``,
``is_favourite`` is a ``novel`` column), FTS is the application-maintained
``novel_search`` derived table (no FTS5 virtual table), and the PRAGMA
kitchen-sink is replaced by PostgreSQL-native checks.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import (
    Author, FailedNovel, Novel, NovelSearch, Tag, TagAlias,
)
from copixiv.features.novels.fts import FTSManager, gram_tokenize


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

    def test_novel_search_requires_valid_novel(self, session_factory):
        """Inserting novel_search with non-existent novel_id should fail."""
        with session_factory() as s:
            s.add(NovelSearch(novel_id=999, search_text="x"))
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
    async def test_delete_novel_cleans_novel_search_and_failed_novel(
        self, session_factory,
    ):
        """Deleting a novel through the repository drops its novel_search row
        (FK CASCADE) and its failure-ledger row (explicit cleanup — the
        ledger has no FK by design)."""
        from copixiv.features.novels.repo import SQLAlchemyNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
            s.flush()
            s.add(NovelSearch(novel_id=1, search_text="t a g"))
            s.add(FailedNovel(
                novel_id=1, failure_type="download", error_message="e",
                failed_times=1, last_failed_at=datetime.now(timezone.utc),
            ))
            s.commit()

            await SQLAlchemyNovelRepository(s).delete(1)
            s.commit()

            assert s.get(NovelSearch, 1) is None
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


class TestNovelSearchFTS:
    def test_batch_rebuild_is_idempotent(self, session_factory):
        with session_factory() as s:
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            fts.batch_rebuild_fts()
            s.commit()

    def test_health_check_empty_db(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            result = fts.check_fts_health()
            assert result["fts_table_exists"] is True
            assert result["is_healthy"] is True
            assert result["novel_count"] == 0
            assert result["fts_entry_count"] == 0

    def test_health_check_with_novel(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="auth"))
            s.flush()
            s.add(Novel(id=1, title="Test", author_id=1, path="/tmp/t.txt"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            result = fts.check_fts_health()
            assert result["is_healthy"] is True
            assert result["novel_count"] == 1
            assert result["fts_entry_count"] == 1
            assert result.get("orphan_entries", 0) == 0

    def test_incremental_update(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="auth"))
            s.flush()
            s.add(Novel(id=1, title="Test Novel", author_id=1, path="/tmp/t.txt"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            s.add(Novel(id=2, title="Second", author_id=1, path="/tmp/t2.txt"))
            s.commit()
            fts.update_novel_fts_index([2])
            s.commit()
            result = fts.check_fts_health()
            assert result["fts_entry_count"] == 2

    def test_keyword_matches_tag_only_text(self, session_factory):
        """Tag-only keywords are searchable through novel_search."""
        from copixiv.features.novels.repo import (
            BaseQueryBuilder, fts_query_to_pg,
        )

        # The repo's tsquery phrase wrapping: bare gram → '...' single-quote phrase.
        tsquery = fts_query_to_pg(BaseQueryBuilder._build_fts_query_string("neko"))
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="作者"))
            s.flush()
            s.add(Novel(id=1, title="无标题的测试小说", author_id=1,
                        path="/tmp/1.txt", tags=["neko", "cyberpunk2077"]))
            s.add(Novel(id=2, title="另一篇测试小说", author_id=1,
                        path="/tmp/2.txt", tags=["日常"]))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()

        with session_factory() as s:
            hits = s.execute(
                text(
                    "SELECT novel_id FROM novel_search "
                    "WHERE to_tsvector('simple', search_text) "
                    "@@ to_tsquery('simple', :q)"
                ),
                {"q": tsquery},
            ).scalars().all()
            assert hits == [1]

    def test_missing_search_row_detected_by_health_check(self, session_factory):
        """A novel without a novel_search row is reported by the health check."""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="作者"))
            s.flush()
            s.add(Novel(id=1, title="标题", author_id=1, path="/tmp/1.txt"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            # Remove just the search row → missing_entries > 0.
            s.execute(text("DELETE FROM novel_search WHERE novel_id = 1"))
            s.commit()
            result = fts.check_fts_health()
            assert result["is_healthy"] is False
            assert result["missing_entries"] >= 1


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

    def test_emoji_maps_to_placeholder(self):
        assert gram_tokenize("😀😀") == "龖 龖"


class TestNeedsRebuild:
    def test_missing_search_rows_needs_rebuild(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
            s.commit()
            assert FTSManager(s).needs_rebuild() is True

    def test_matching_counts_no_rebuild(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            assert fts.needs_rebuild() is False

    def test_count_mismatch_needs_rebuild(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
            s.commit()
            fts = FTSManager(s)
            fts.batch_rebuild_fts()
            s.commit()
            assert fts.needs_rebuild() is False
            # Add a novel WITHOUT building its novel_search row → mismatch.
            s.add(Novel(id=2, title="T2", author_id=1, path="/tmp/t2.txt"))
            s.commit()
            assert fts.needs_rebuild() is True


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
