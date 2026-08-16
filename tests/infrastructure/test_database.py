"""Integration tests for database engine, models, and integrity (in-memory SQLite)."""

import pytest
from sqlalchemy import create_engine, text, event
from sqlalchemy.exc import IntegrityError

from copixiv.infrastructure.database.models import (
    Base, Novel, Author, Favourite, Tag, TagAlias, NovelTag,
)
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.repositories.fts import FTSManager


@pytest.fixture
def engine():
    """In-memory SQLite engine with all tables created and FKs enabled."""
    eng = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def session(engine):
    sf = create_session_factory(engine)
    s = sf()
    yield s
    s.close()


class TestModels:
    def test_create_author(self, session):
        a = Author(author_id=1, author_name="test")
        session.add(a)
        session.commit()

        result = session.get(Author, 1)
        assert result.author_name == "test"

    def test_create_novel(self, session):
        a = Author(author_id=10, author_name="auth")
        session.add(a)
        n = Novel(id=100, title="Novel", author_id=10, path="/tmp/test.txt")
        session.add(n)
        session.commit()

        result = session.get(Novel, 100)
        assert result.title == "Novel"
        assert result.author_id == 10

    def test_favourite_unique(self, session):
        a = Author(author_id=1, author_name="a")
        n = Novel(id=1, title="T", author_id=1, path="/tmp/t.txt")
        session.add_all([a, n])
        session.flush()

        f1 = Favourite(novel_id=1)
        session.add(f1)
        session.commit()
        # Detach f1 so the second insert is a fresh row (no identity-map
        # conflict warning) and the PK uniqueness is enforced by SQLite.
        session.expunge(f1)

        f2 = Favourite(novel_id=1)
        session.add(f2)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestForeignKeyIntegrity:
    """Phase 4: Verify foreign key constraints actually work."""

    def test_novel_requires_valid_author(self, session):
        """Inserting a novel with non-existent author_id should fail."""
        n = Novel(id=1, title="Orphan", author_id=999, path="/tmp/orphan.txt")
        session.add(n)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_novel_tag_requires_valid_novel(self, session):
        """Inserting novel_tag with non-existent novel_id should fail."""
        session.add(Tag(name="test_tag", reference_count=0))
        session.flush()
        nt = NovelTag(novel_id=999, tag_id=1)
        session.add(nt)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_novel_tag_requires_valid_tag(self, session):
        """Inserting novel_tag with non-existent tag_id should fail."""
        session.add(Author(author_id=1, author_name="a"))
        session.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
        session.flush()
        nt = NovelTag(novel_id=1, tag_id=999)
        session.add(nt)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_tag_alias_requires_valid_source_tag(self, session):
        """Inserting tag_alias with non-existent source should fail."""
        session.add(Tag(name="valid_tag", reference_count=0))
        session.flush()
        ta = TagAlias(source=1, target=999)
        session.add(ta)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_tag_alias_requires_valid_target_tag(self, session):
        """Inserting tag_alias with non-existent target should fail."""
        session.add(Tag(name="valid_tag", reference_count=0))
        session.flush()
        ta = TagAlias(source=999, target=1)
        session.add(ta)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_cascade_delete_novel_cleans_novel_tag(self, session):
        """Deleting a novel should cascade-delete its novel_tag rows."""
        session.add(Author(author_id=1, author_name="a"))
        session.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
        session.add(Tag(name="tag1", reference_count=0))
        session.flush()
        session.add(NovelTag(novel_id=1, tag_id=1))
        session.commit()

        # Verify novel_tag exists
        count = session.query(NovelTag).filter_by(novel_id=1).count()
        assert count == 1

        # Delete novel -> cascade delete novel_tag
        novel = session.get(Novel, 1)
        session.delete(novel)
        session.commit()

        count = session.query(NovelTag).filter_by(novel_id=1).count()
        assert count == 0


class TestIndexesExist:
    """Phase 4: Verify that expected indexes are present."""

    EXPECTED_INDEXES = {
        # Standard indexes from ORM models
        "ix_novel_like",
        "ix_novel_text",
        "ix_novel_has_epub",
        "ix_novel_create_time",
        "idx_novel_author_likes",
        "idx_novel_series_likes",
        "idx_novel_like_text_id",
        "idx_novel_author_id",
        "ix_novel_shuffle_like_text",
        "ix_novel_shuffle_id",
        "idx_novel_tag_tag_id",
        "idx_novel_tag_novel_id",
        "ix_search_history_timestamp",
        "ix_search_history_type_timestamp",
        "ix_tag_aliases_source",
        "ix_tag_aliases_target",
        "ix_tag_preferences_tag",
    }

    def test_all_expected_indexes_exist(self, engine):
        """Verify all expected indexes are present in the schema.

        Note: Unique constraints may be created as sqlite_autoindex_* by
        create_all() rather than explicit index names.  This test only
        checks explicit named indexes.
        """
        from sqlalchemy import inspect
        insp = inspect(engine)

        actual_names: set[str] = set()
        for table_name in insp.get_table_names():
            for idx in insp.get_indexes(table_name):
                # Skip sqlite_autoindex_* (implicit unique constraint indexes)
                if not idx["name"].startswith("sqlite_autoindex_"):
                    actual_names.add(idx["name"])

        missing = self.EXPECTED_INDEXES - actual_names
        assert not missing, f"Missing indexes: {missing}"


class TestFTS:
    """Phase 2: FTS5 health checks and operations."""

    def test_create_fts_if_not_exists(self, session):
        """rebuild_novel_fts should be idempotent."""
        fts = FTSManager(session)
        # First call — creates FTS
        fts.rebuild_novel_fts()
        # Second call — should not raise
        fts.rebuild_novel_fts()

    def test_fts_health_check_empty_db(self, session):
        """Health check should report status on empty DB."""
        fts = FTSManager(session)
        fts.rebuild_novel_fts()
        result = fts.check_fts_health()
        assert result["fts_table_exists"] is True
        assert result["is_healthy"] is True
        assert result["novel_count"] == 0
        assert result["fts_entry_count"] == 0

    def test_fts_health_check_with_novel(self, session):
        """Health check should detect no orphans when everything matches."""
        session.add(Author(author_id=1, author_name="auth"))
        session.add(Novel(id=1, title="Test", author_id=1, path="/tmp/t.txt"))
        session.commit()

        fts = FTSManager(session)
        fts.rebuild_novel_fts()
        result = fts.check_fts_health()
        assert result["is_healthy"] is True
        assert result["novel_count"] == 1
        assert result["fts_entry_count"] == 1
        assert result.get("orphan_entries", 0) == 0

    def test_fts_incremental_update(self, session):
        """Incremental FTS update should work for new novels."""
        session.add(Author(author_id=1, author_name="auth"))
        session.add(Novel(id=1, title="Test Novel", author_id=1, path="/tmp/t.txt"))
        session.commit()

        fts = FTSManager(session)
        fts.rebuild_novel_fts()

        # Add another novel and update incrementally
        session.add(Novel(id=2, title="Second", author_id=1, path="/tmp/t2.txt"))
        session.commit()
        fts.update_novel_fts_index([2])

        result = fts.check_fts_health()
        assert result["fts_entry_count"] == 2


class TestFtsTagsIndexing:
    """T5: keyword search must hit text that exists ONLY in tags.

    Regression guard for the D1 backfill: a v1-era ``novel_fts`` table has
    no tags column, so tag-only keywords silently match nothing until the
    index is rebuilt.  These tests pin that after a rebuild, the real
    repository search path (query builder + MATCH) finds tag-only text.
    """

    @pytest.fixture
    def repo_session(self):
        """Session on a StaticPool in-memory DB — repository queries run in
        worker threads, so the plain :memory: engine (single-threaded,
        per-connection DBs) cannot be reused here."""
        from sqlalchemy.pool import StaticPool
        eng = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=eng)
        sf = create_session_factory(eng)
        s = sf()
        yield s
        s.close()

    def test_rebuild_fts_resets_availability_cache(self, repo_session):
        """A runtime rebuild must re-enable keyword filtering even when the
        process started while the FTS table was missing (the availability
        cache was probed False and would otherwise stay False until restart)."""
        import asyncio
        from copixiv.infrastructure.repositories import query_builder_base as qbb
        from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository

        try:
            qbb._fts_available = False  # simulate a start without the table
            assert qbb._check_fts_available(repo_session) is False

            asyncio.run(SQLAlchemyNovelRepository(repo_session).rebuild_fts())

            assert qbb._fts_available is None, "cache must be invalidated"
            assert qbb._check_fts_available(repo_session) is True
        finally:
            qbb.reset_fts_cache()

    @staticmethod
    def _seed_tagged_novel(session, novel_id: int, title: str, tag_names: list[str]):
        """Insert a novel whose ONLY text containing the keyword is in tags."""
        session.add(Author(author_id=novel_id, author_name="作者"))
        session.flush()
        session.add(Novel(
            id=novel_id, title=title, author_id=novel_id,
            author_name="作者", path=f"/tmp/{novel_id}.txt",
        ))
        session.flush()
        for name in tag_names:
            tag = Tag(name=name, reference_count=1)
            session.add(tag)
            session.flush()
            session.add(NovelTag(novel_id=novel_id, tag_id=tag.id))
        session.commit()

    @staticmethod
    def _keyword_ids(session, keyword: str) -> list[int]:
        import asyncio
        from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository
        from copixiv.infrastructure.repositories.query_builder_base import reset_fts_cache
        # The FTS availability cache is process-wide — a previous test may
        # have cached False for a DB without the virtual table.
        reset_fts_cache()
        repo = SQLAlchemyNovelRepository(session)
        result = asyncio.run(repo.get_novels(
            conditions=[("keyword", keyword)], order_by="id", per_page=50,
        ))
        return [n["id"] for n in result["novels"]]

    def test_keyword_matches_tag_only_text(self, repo_session):
        """D1 regression: tags are searchable after an FTS rebuild."""
        # The keyword appears in NO other indexed column (title/author/series).
        self._seed_tagged_novel(repo_session, 1, "无标题的测试小说", ["neko", "cyberpunk2077"])
        self._seed_tagged_novel(repo_session, 2, "另一篇测试小说", ["日常"])

        fts = FTSManager(repo_session)
        fts.rebuild_novel_fts()
        repo_session.commit()  # production contract: caller commits the rebuild

        assert self._keyword_ids(repo_session, "neko") == [1]
        assert self._keyword_ids(repo_session, "cyberpunk2077") == [1]
        assert self._keyword_ids(repo_session, "日常") == [2]
        assert self._keyword_ids(repo_session, "完全不存在") == []

    def test_keyword_matches_title_and_tags_independently(self, repo_session):
        """Sanity: title-only hits still work alongside tag-only hits."""
        self._seed_tagged_novel(repo_session, 1, "neko 标题", ["日常"])
        self._seed_tagged_novel(repo_session, 2, "其他标题", ["neko"])

        fts = FTSManager(repo_session)
        fts.rebuild_novel_fts()
        repo_session.commit()

        assert sorted(self._keyword_ids(repo_session, "neko")) == [1, 2]

    def test_orphan_fts_row_detected_by_health_check(self, repo_session):
        """check_fts_health reports orphan entries (FTS row without a novel)."""
        self._seed_tagged_novel(repo_session, 1, "标题", ["neko"])
        fts = FTSManager(repo_session)
        fts.rebuild_novel_fts()
        repo_session.commit()

        # Remove the novel row directly, leaving its FTS entry orphaned.
        novel = repo_session.get(Novel, 1)
        repo_session.delete(novel)
        repo_session.commit()

        result = fts.check_fts_health()
        assert result["is_healthy"] is False
        assert result["orphan_entries"] >= 1


class TestConnectionPoolConfig:
    """Phase 4: Verify connection pool configuration."""

    def test_foreign_keys_pragma_enabled(self, engine):
        """Verify foreign_keys PRAGMA is ON for new connections."""
        with engine.connect() as conn:
            fk_status = conn.execute(text("PRAGMA foreign_keys")).scalar()
            assert fk_status == 1, "foreign_keys PRAGMA should be ON"

    def test_busy_timeout_set(self, engine):
        """Verify busy_timeout PRAGMA is set."""
        with engine.connect() as conn:
            timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
            assert timeout > 0, "busy_timeout should be > 0"
