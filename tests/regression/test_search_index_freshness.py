"""Regression pins for keyword-search index freshness.

History: the search index used to be an application-maintained
``novel_search`` table that five repository call sites refreshed by hand.
``SQLAlchemyAuthorRepository.update_author_name`` — the author-name writeback
that runs after every webview download (the download draft is built with
``author_name=None``) — was not one of them, so a freshly downloaded novel was
indexed without its author name and stayed unsearchable by author until a
manual rebuild.

The index is now an expression index over ``novel`` (migration 0003), so
PostgreSQL derives it from the row being written.  These tests pin that
guarantee at the places where the old design broke: they never call an index
refresh helper, and every write path below must nevertheless be searchable.
"""

import pytest
from sqlalchemy import text

from copixiv.core.services import QuerySpec, parse_search_keyword
from copixiv.db.models import Author, Novel
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


async def _search(session, keyword: str) -> list[int]:
    res = await SQLAlchemyNovelRepository(session).get_novels(
        QuerySpec(
            conditions=parse_search_keyword(f"keyword:{keyword}"),
            per_page=50, exclude_blocked_tags=False,
        )
    )
    return [n.id for n in res["novels"]]


@pytest.fixture
def nameless_novel(session_factory):
    """A novel exactly as the webview download path creates it: no author name."""
    with session_factory() as s:
        s.add(Author(author_id=77))  # placeholder row, author_name IS NULL
        s.flush()
        s.add(Novel(
            id=700, title="无名的书", author_id=77, author_name=None,
            tags=["幻想"],
        ))
        s.commit()
    return 700


async def test_author_name_writeback_is_immediately_searchable(
    session_factory, nameless_novel,
):
    """``update_author_name`` must not leave the search index behind."""
    with session_factory() as s:
        assert await _search(s, "作者甲") == []

    with session_factory() as s:
        await SQLAlchemyAuthorRepository(s).update_author_name(77, "作者甲")
        s.commit()

    with session_factory() as s:
        assert await _search(s, "作者甲") == [nameless_novel]


async def test_author_rename_is_immediately_searchable(
    session_factory, nameless_novel,
):
    """A rename updates both the new and the old keyword."""
    with session_factory() as s:
        await SQLAlchemyAuthorRepository(s).update_author_name(77, "旧名")
        s.commit()

    with session_factory() as s:
        await SQLAlchemyAuthorRepository(s).update_author_name(77, "新名")
        s.commit()

    with session_factory() as s:
        assert await _search(s, "新名") == [nameless_novel]
        assert await _search(s, "旧名") == []


async def test_plain_sql_update_is_searchable(session_factory, nameless_novel):
    """The guarantee is the database's, not the repository's.

    A direct ``UPDATE`` (psql, migration script, future writer) is indexed too,
    which is what makes the drift class impossible rather than merely fixed.
    """
    with session_factory() as s:
        s.execute(
            text("UPDATE novel SET title = '改名后的标题' WHERE id = :id"),
            {"id": nameless_novel},
        )
        s.commit()

    with session_factory() as s:
        assert await _search(s, "改名后的标题") == [nameless_novel]
        assert await _search(s, "无名的书") == []


async def test_tag_change_is_searchable(session_factory, nameless_novel):
    """Tag edits (a search segment) are indexed without a refresh call."""
    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).add_tags_to_novels(
            [nameless_novel], {"新标签"},
        )
        s.commit()

    with session_factory() as s:
        assert await _search(s, "新标签") == [nameless_novel]


async def test_series_name_is_searchable(session_factory, nameless_novel):
    """Series name is part of the indexed text and follows row updates."""
    with session_factory() as s:
        s.execute(
            text("UPDATE novel SET series_name = '系列甲' WHERE id = :id"),
            {"id": nameless_novel},
        )
        s.commit()

    with session_factory() as s:
        assert await _search(s, "系列甲") == [nameless_novel]
