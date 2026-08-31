"""PostgreSQL infrastructure smoke tests (phase 1).

These verify the three properties that the new PG foundation must guarantee:

1. The ``sync_tag_refs`` trigger maintains ``tag.reference_count`` exactly
   (one count per novel per tag, no double-count on duplicate array elements).
2. The ``novel_search`` GIN index answers char-gram phrase queries.
3. The partial indexes / GIN index exist with the right definitions.
"""

from pathlib import Path

import pytest
from sqlalchemy import select, text

from copixiv.db.models import Author, Novel, Tag, NovelSearch
from copixiv.features.novels.fts import gram_tokenize


@pytest.fixture
def _author(session_factory):
    with session_factory() as s:
        s.add(Author(author_id=1, author_name="alice"))
        s.commit()


def test_trigger_reference_count(session_factory, clean_db, _author):
    """Duplicate tags in a novel.tags array count once; distinct novels each count."""
    with session_factory() as s:
        s.add(Novel(id=1, title="n1", author_id=1,
                    tags=["R-18", "中文", "R-18"], is_favourite=True))
        s.add(Novel(id=2, title="n2", author_id=1, tags=["中文"]))
        s.commit()

    with session_factory() as s:
        rc = dict(s.execute(select(Tag.name, Tag.reference_count)).all())

    assert rc["R-18"] == 1, "duplicate array element must not double-count"
    assert rc["中文"] == 2, "each distinct novel must contribute one count"


def test_novel_search_gin_phrase(session_factory, clean_db, _author):
    """The GIN index over to_tsvector('simple', search_text) answers a char-gram query."""
    search_text = gram_tokenize("催眠の誘い 作者 系列 R-18")
    with session_factory() as s:
        s.add(Novel(id=10, title="催眠の誘い", author_id=1, tags=["R-18"]))
        s.flush()
        s.add(NovelSearch(novel_id=10, search_text=search_text))
        s.commit()

    with session_factory() as s:
        cnt = s.execute(
            text(
                "SELECT count(*) FROM novel_search "
                "WHERE to_tsvector('simple', search_text) "
                "@@ to_tsquery('simple', '催')"
            )
        ).scalar()

    assert cnt >= 1, "gram phrase query should hit the inserted row"


def test_partial_and_gin_indexes(pg_engine, clean_db):
    """Required partial indexes and the GIN index exist with the right definitions."""

    def indexdef(name: str):
        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND indexname=:n"
                ),
                {"n": name},
            ).fetchone()
            return row[0] if row else None

    fav = indexdef("ix_novel_favourite")
    assert fav and "WHERE is_favourite" in fav, "novel favourite partial index"

    sf = indexdef("ix_author_special_follow")
    assert sf and "WHERE is_special_follow" in sf, "author special-follow partial index"

    running = indexdef("ux_task_history_running")
    assert running and "WHERE" in running and "status" in running, \
        "task_history partial unique index (predicate normalized to = ANY(...))"

    gin = indexdef("novel_search_gin")
    assert gin and "USING gin" in gin, "novel_search GIN index"
