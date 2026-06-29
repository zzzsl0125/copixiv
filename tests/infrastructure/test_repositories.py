"""Integration tests for repositories (in-memory SQLite)."""

import asyncio
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from copixiv.infrastructure.database.models import (
    Base, Novel, Author, Tag, TagAlias, Favourite,
)
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.repositories.author import AuthorRepository
from copixiv.infrastructure.repositories.tag import TagRepository


@pytest.fixture
def engine():
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


class TestNovelRepository:
    def test_upsert_new_novel(self, session):
        session.add(Author(author_id=10, author_name="Test Author"))
        session.commit()

        repo = NovelRepository(session)
        count = asyncio.run(repo.upsert_novels([{
            "id": 1, "title": "Test Novel", "author_id": 10,
            "path": "/tmp/n1.txt", "text": 5000,
            "tag": ["R-18", "NTR"],
        }]))
        session.commit()
        assert count == 1

        novel = session.get(Novel, 1)
        assert novel is not None
        assert novel.title == "Test Novel"

    def test_get_existing_ids(self, session):
        session.add(Author(author_id=1, author_name="Test Author"))
        session.add(Novel(id=1, title="A", author_id=1, path="/tmp/a.txt"))
        session.add(Novel(id=2, title="B", author_id=1, path="/tmp/b.txt"))
        session.commit()

        repo = NovelRepository(session)
        existing = asyncio.run(repo.get_existing_ids({1, 2, 3}))
        assert existing == {1, 2}

    def test_toggle_favourite(self, session):
        session.add(Author(author_id=1, author_name="Test Author"))
        session.add(Novel(id=1, title="F", author_id=1, path="/tmp/f.txt"))
        session.commit()

        repo = NovelRepository(session)

        asyncio.run(repo.toggle_favourite(1))
        session.commit()

        fav = session.get(Favourite, 1)
        assert fav is not None

        asyncio.run(repo.toggle_favourite(1))
        session.commit()
        assert session.get(Favourite, 1) is None


class TestAuthorRepository:
    def test_get_by_id(self, session):
        session.add(Author(author_id=99, author_name="Test Author"))
        session.commit()

        repo = AuthorRepository(session)
        author = asyncio.run(repo.get_by_id(99))
        assert author is not None
        assert author["author_name"] == "Test Author"

    def test_need_update_new_author(self, session):
        repo = AuthorRepository(session)
        assert asyncio.run(repo.need_update(999)) is True

    def test_need_update_with_date(self, session):
        from datetime import date
        session.add(Author(
            author_id=5, author_name="Old",
            last_update=date.today().isoformat(),
        ))
        session.commit()

        repo = AuthorRepository(session)
        assert asyncio.run(repo.need_update(5)) is False


class TestTagRepository:
    """Phase 3: Tests for tag aliases with integer FK normalization."""

    def _ensure_tags(self, session, *names: str) -> dict[str, int]:
        """Ensure tags exist and return {name: id} mapping."""
        repo = TagRepository(session)
        result = {}
        for name in names:
            tid = repo._get_or_create_tag_id(name)
            result[name] = tid
        session.flush()
        return result

    def test_get_alias_map_with_fk_tags(self, session):
        """get_alias_map should return {source_name: target_name} via FK join."""
        ids = self._ensure_tags(session, "R-18", "R18", "NTR", "ntr")

        session.add(TagAlias(source=ids["R-18"], target=ids["R18"]))
        session.add(TagAlias(source=ids["NTR"], target=ids["ntr"]))
        session.commit()

        repo = TagRepository(session)
        alias_map = repo.get_alias_map_sync()
        assert alias_map.get("R-18") == "R18"
        assert alias_map.get("NTR") == "ntr"

    def test_create_alias_via_repository(self, session):
        """create_alias should accept tag names and store as integer FKs."""
        ids = self._ensure_tags(session, "source_tag", "target_tag")

        repo = TagRepository(session)
        result = asyncio.run(repo.create_alias({
            "source": "source_tag",
            "target": "target_tag",
        }))
        session.commit()

        assert result["source"] == "source_tag"
        assert result["target"] == "target_tag"
        assert result["id"] is not None

        # Verify stored as integer FKs
        alias = session.get(TagAlias, result["id"])
        assert alias.source == ids["source_tag"]
        assert alias.target == ids["target_tag"]

    def test_alias_fk_integrity(self, session):
        """Inserting alias with non-existent tag ID should fail."""
        session.add(Tag(name="valid", reference_count=0))
        session.flush()

        session.add(TagAlias(source=1, target=999))
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    def test_unique_source_constraint(self, session):
        """Two aliases with the same source should fail."""
        ids = self._ensure_tags(session, "src", "tgt1", "tgt2")

        session.add(TagAlias(source=ids["src"], target=ids["tgt1"]))
        session.commit()

        session.add(TagAlias(source=ids["src"], target=ids["tgt2"]))
        with pytest.raises(Exception):
            session.commit()
        session.rollback()


@pytest.mark.slow
class TestConcurrentAccess:
    """Phase 4: Concurrent access should not cause 'database is locked'."""

    @pytest.fixture
    def file_engine(self, tmp_path):
        """File-based engine for cross-thread access."""
        import tempfile
        db_path = tmp_path / "test_concurrent.db"
        eng = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        from sqlalchemy import event
        @event.listens_for(eng, "connect")
        def _set_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(bind=eng)
        return eng

    def test_concurrent_reads(self, file_engine):
        """10 threads reading simultaneously should not error."""
        import concurrent.futures

        # Pre-populate
        Session = create_session_factory(file_engine)
        with Session() as s:
            s.add(Author(author_id=1, author_name="concurrent_test"))
            s.flush()
            for i in range(100):
                s.add(Novel(id=i+1, title=f"N{i}", author_id=1,
                            path=f"/tmp/c{i}.txt"))
            s.commit()

        errors = []

        def read_page(offset: int):
            try:
                with Session() as s:
                    repo = NovelRepository(s)
                    result = asyncio.run(repo.get_novels(
                        order_by="id",
                        per_page=10,
                    ))
                    return len(result.get("novels", []))
            except Exception as e:
                errors.append(str(e))
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(read_page, i * 10) for i in range(10)]
            results = [f.result() for f in futures]

        assert not errors, f"Concurrent reads failed: {errors}"
        assert all(r == 10 for r in results[:9]), \
            f"Expected 10 novels per page, got {results}"
