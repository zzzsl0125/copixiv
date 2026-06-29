"""Integration tests for database engine, models, and integrity (in-memory SQLite)."""

import pytest
from sqlalchemy import create_engine, text, event

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

        f2 = Favourite(novel_id=1)
        session.add(f2)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()


class TestForeignKeyIntegrity:
    """Phase 4: Verify foreign key constraints actually work."""

    def test_novel_requires_valid_author(self, session):
        """Inserting a novel with non-existent author_id should fail."""
        n = Novel(id=1, title="Orphan", author_id=999, path="/tmp/orphan.txt")
        session.add(n)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    def test_novel_tag_requires_valid_novel(self, session):
        """Inserting novel_tag with non-existent novel_id should fail."""
        session.add(Tag(name="test_tag", reference_count=0))
        session.flush()
        nt = NovelTag(novel_id=999, tag_id=1)
        session.add(nt)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    def test_novel_tag_requires_valid_tag(self, session):
        """Inserting novel_tag with non-existent tag_id should fail."""
        session.add(Author(author_id=1, author_name="a"))
        session.add(Novel(id=1, title="T", author_id=1, path="/tmp/t.txt"))
        session.flush()
        nt = NovelTag(novel_id=1, tag_id=999)
        session.add(nt)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    def test_tag_alias_requires_valid_source_tag(self, session):
        """Inserting tag_alias with non-existent source should fail."""
        session.add(Tag(name="valid_tag", reference_count=0))
        session.flush()
        ta = TagAlias(source=1, target=999)
        session.add(ta)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    def test_tag_alias_requires_valid_target_tag(self, session):
        """Inserting tag_alias with non-existent target should fail."""
        session.add(Tag(name="valid_tag", reference_count=0))
        session.flush()
        ta = TagAlias(source=999, target=1)
        session.add(ta)
        with pytest.raises(Exception):
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
        "idx_novel_like_id",
        "idx_novel_tag_tag_id",
        "idx_novel_tag_novel_id",
        "idx_random_pool_criteria",
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
