"""Database integrity / search-index / index tests for the PostgreSQL foundation.

Post-migration rewrite of the SQLite-era module: the ``novel_tag`` /
``favourite`` join tables are gone (tags live in ``novel.tags text[]``,
``is_favourite`` is a ``novel`` column), keyword search is a ``pg_bigm`` GIN
index over the generated ``novel.search_lower`` column (no FTS5 virtual table,
no derived search table, no char-gram expression index), and the PRAGMA
kitchen-sink is replaced by PostgreSQL-native checks.

Search behaviour (index definition, predicate construction, case folding,
freshness) lives in ``tests/features/test_search_index.py``,
``tests/adapters/test_query_builder.py`` and
``tests/regression/test_search_index_freshness.py``.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import (
    Author, FailedNovel, Novel, Tag, TagAlias,
)


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
        needs no cleanup: it covers the generated ``search_lower`` column."""
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
        "novel_search_bigm",
        "ix_failed_novel_last_failed_at",
        # migration 0008 — tag alias suggestion path
        "tag_search_bigm",
        "tag_reference_count_id_idx",
    }

    def test_all_expected_indexes_exist(self, pg_engine):
        with pg_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
            ).scalars().all()
        actual = set(rows)
        missing = self.EXPECTED_INDEXES - actual
        assert not missing, f"Missing indexes: {missing}"


class TestTagSearchLowerColumn:
    """``tag.search_lower`` — the generated column the suggestion path uses."""

    def test_column_is_a_stored_generated_column(self, pg_engine):
        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT is_generated, generation_expression "
                    "FROM information_schema.columns "
                    "WHERE table_name='tag' AND column_name='search_lower'"
                )
            ).first()
        assert row is not None, "tag.search_lower must exist (migration 0008)"
        assert row[0] == "ALWAYS"
        # Unlike novel's copixiv_search_text (which wraps the STABLE
        # concat_ws / array_to_string), lower(text) is catalogued IMMUTABLE —
        # so the expression is inlined, with no wrapper function.
        assert row[1] == "lower(name)"

    def test_generated_column_follows_the_name(self, session_factory):
        with session_factory() as s:
            s.add(Tag(name="BanG_Dream!", reference_count=0))
            s.commit()
            value = s.execute(
                text("SELECT search_lower FROM tag WHERE name=:n"),
                {"n": "BanG_Dream!"},
            ).scalar()
        assert value == "bang_dream!"


class TestSearchLowerColumn:
    """The generated search-text column the pg_bigm index covers."""

    def test_column_is_a_stored_generated_column(self, pg_engine):
        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT is_generated, generation_expression "
                    "FROM information_schema.columns "
                    "WHERE table_name='novel' AND column_name='search_lower'"
                )
            ).first()
        assert row is not None, "novel.search_lower must exist (migration 0007)"
        assert row[0] == "ALWAYS"
        # The assembly lives in an IMMUTABLE SQL function (a STORED generated
        # column cannot call the STABLE concat_ws / array_to_string directly).
        assert "copixiv_search_text" in row[1]

    def test_assembly_function_lowercases_all_four_fields(self, session_factory):
        """``copixiv_search_text`` = ``lower(concat_ws(' ', parts))``."""
        with session_factory() as s:
            value = s.execute(
                text(
                    "SELECT copixiv_search_text("
                    ":title, :author, :series, :tags)"
                ),
                {
                    "title": "Title", "author": "AuthorName",
                    "series": "Series 1", "tags": ["TagA", "TagB"],
                },
            ).scalar()
        assert value == "title authorname series 1 taga tagb"

    def test_assembly_function_returns_empty_for_empty_parts(self, session_factory):
        """All fields empty → ``''`` (``concat_ws`` skips NULLs) — never NULL."""
        with session_factory() as s:
            for args in (
                {"title": "", "author": "", "series": "", "tags": []},
                {"title": "", "author": None, "series": None, "tags": []},
            ):
                value = s.execute(
                    text(
                        "SELECT copixiv_search_text("
                        ":title, :author, :series, :tags)"
                    ),
                    args,
                ).scalar()
                assert value == ""

    def test_generation_matches_the_four_search_fields(self, session_factory):
        """title / author / series / tags, space-joined and lowercased."""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="AuthorName"))
            s.flush()
            s.add(Novel(
                id=1, title="Title", author_id=1, author_name="AuthorName",
                series_name="Series 1", tags=["TagA", "TagB"],
            ))
            s.commit()
            value = s.execute(
                text("SELECT search_lower FROM novel WHERE id=1")
            ).scalar()
        assert value == "title authorname series 1 taga tagb"

    def test_empty_optional_fields_do_not_add_separators(self, session_factory):
        """``nullif(...,'')`` keeps empty fields from shifting bigram positions."""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="a"))
            s.flush()
            s.add(Novel(
                id=2, title="OnlyTitle", author_id=1, author_name="",
                series_name=None, tags=[],
            ))
            s.commit()
            value = s.execute(
                text("SELECT search_lower FROM novel WHERE id=2")
            ).scalar()
        assert value == "onlytitle"


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
