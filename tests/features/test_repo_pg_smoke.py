"""Repository-layer PostgreSQL functional smoke tests (phase 2-B1).

Each assertion runs against a real 200-novel sample seeded from the SQLite
source (the session-scoped ``seeded_db`` fixture runs
``scripts/migrate_sqlite_to_pg.py --limit 200 --reset``).  Expectations are
never hard-coded where the sample could vary — the preferred form is to
compare the repository's answer to a directly-executed SQL reference, or to
assert an invariant (count consistency, toggle idempotency, no keyset
duplication, FK/trigger correctness).

The mutable tests (blocked-tag preference, toggle_favourite, add/remove
tags, delete_many) restore the state they change (or operate on dedicated
rows), so they stay independent of one another within the session.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text, delete as _delete

from copixiv.core.services import QuerySpec, parse_search_keyword
from copixiv.db.models import (
    Author, Novel, FailedNovel, Tag, TagPreference,
)
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository


def _repo(session):
    return SQLAlchemyNovelRepository(session)


def _sql(seeded_db, sql, params=None):
    with seeded_db.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _sql_scalar(seeded_db, sql, params=None):
    with seeded_db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


async def test_get_novels_default_list(seeded_db, session_factory):
    """Default ordering + threshold returns Novel objects with tags/is_favourite."""
    with session_factory() as s:
        res = await _repo(s).get_novels(QuerySpec(per_page=50))
    novels = res["novels"]
    assert len(novels) == 50
    n0 = novels[0]
    assert isinstance(n0.tags, list)
    assert isinstance(n0.is_favourite, bool)
    assert isinstance(n0.create_time, datetime)
    # cursor present (we fetched more than per_page).
    assert res["cursor"] is not None


async def test_tags_filter(seeded_db, session_factory):
    """Tag filter (tags:R-18) matches every returned novel carrying R-18."""
    with session_factory() as s:
        res = await _repo(s).get_novels(
            QuerySpec(conditions=parse_search_keyword("tags:R-18"), per_page=50)
        )
    assert len(res["novels"]) > 0
    assert all("R-18" in n.tags for n in res["novels"])
    # Reference count of "R-18" in the seed is >0.
    assert _sql_scalar(
        seeded_db, "SELECT reference_count FROM tag WHERE name=:n", {"n": "R-18"}
    ) > 0


async def test_keyword_search_substring(seeded_db, session_factory):
    """Keyword search hits and the SQL is a substring match on search_lower."""
    with session_factory() as s:
        res = await _repo(s).get_novels(
            QuerySpec(
                conditions=parse_search_keyword("keyword:催"), per_page=100,
                exclude_blocked_tags=False,
            )
        )
    # Reference: the same predicate evaluated straight in SQL.
    ref = _sql_scalar(
        seeded_db,
        "SELECT count(*) FROM novel WHERE search_lower LIKE '%催%'",
    )
    assert ref > 0
    assert len(res["novels"]) == ref  # list is unpaged up to per_page=100


async def test_keyword_search_rare_term(seeded_db, session_factory):
    """A rare/no-hit keyword is a valid filter (≥0), no SQL error."""
    with session_factory() as s:
        res = await _repo(s).get_novels(
            QuerySpec(conditions=parse_search_keyword("keyword:哈利波特"), per_page=20)
        )
    assert 0 <= len(res["novels"]) <= 20


async def test_keyset_pagination_page2(seeded_db, session_factory):
    """Second keyset page has no duplication and no missing rows."""
    with session_factory() as s:
        repo = _repo(s)
        page1 = await repo.get_novels(
            QuerySpec(order_by="like", order_direction="DESC", per_page=20)
        )
        page2 = await repo.get_novels(
            QuerySpec(
                order_by="like", order_direction="DESC", per_page=20,
                cursor=page1["cursor"],
            )
        )
        # 参照：同一个查询一次取 40 —— 两页拼起来必须**逐位相等**。
        # （只查重复是不够的：游标取多取的那行探测行时，它会在下一页被严格
        # 比较排除，每页静默丢一本，而「无重复」照样成立。）
        ref = await repo.get_novels(
            QuerySpec(order_by="like", order_direction="DESC", per_page=40)
        )
    ids1 = {n.id for n in page1["novels"]}
    ids2 = {n.id for n in page2["novels"]}
    assert len(page1["novels"]) == 20
    assert len(page2["novels"]) == 20
    assert not (ids1 & ids2), "page 2 must not repeat page 1 rows"
    assert [n.id for n in page1["novels"]] + [n.id for n in page2["novels"]] == [
        n.id for n in ref["novels"]
    ], "两页合起来必须正好是前 40 本，不重不漏"


async def test_random_browse(seeded_db, session_factory):
    """Random (shuffle) browse returns the full page and a cursor."""
    with session_factory() as s:
        res = await _repo(s).get_novels(
            QuerySpec(order_by="random", per_page=30)
        )
    assert len(res["novels"]) == 30
    assert res["cursor"] is not None
    assert res["cursor"]["id"] == res["novels"][-1].id


async def test_count_novels_list_consistency_and_excluded(seeded_db, session_factory):
    """count_novels matches list length; blocked-tag exclusion is exact.

    计数现在是有界的（``NovelCount``），但样本（200 本）远小于
    ``COUNT_CAP``，所以这里的值仍然精确、``truncated`` 为假 —— 有界语义本身
    由 ``tests/features/test_novel_count_cap.py`` 覆盖。
    """
    with session_factory() as s:
        repo = _repo(s)
        trepo = SQLAlchemyTagRepository(s)
        blocked_tag = "R-18"

        total = await repo.count_novels(QuerySpec(exclude_blocked_tags=False))
        ids = await repo.list_matching_ids(QuerySpec(exclude_blocked_tags=False))
        assert total.truncated is False
        assert total.value == len(ids)
        assert total.value > 0

        # Block R-18 and verify excluded + visible == total.
        await trepo.create_preference(
            {"tag": blocked_tag, "preference": "blocked"}
        )
        s.commit()
        try:
            visible = await repo.count_novels(QuerySpec())
            excluded = await repo.count_excluded_novels(QuerySpec())
            assert visible.truncated is False and excluded.truncated is False
            assert visible.value + excluded.value == total.value
            assert excluded.value > 0
            # A novel carrying blocked tag is not visible.
            blocked_id = _sql_scalar(
                seeded_db,
                "SELECT id FROM novel WHERE tags @> ARRAY[:t] LIMIT 1",
                {"t": blocked_tag},
            )
            if blocked_id:
                hidden = await repo.get_by_id(blocked_id)
                list_ids = await repo.list_matching_ids(QuerySpec())
                assert hidden.id not in list_ids
        finally:
            # Restore the global setting state.
            s.execute(
                _delete(TagPreference).where(
                    TagPreference.tag == blocked_tag,
                    TagPreference.preference == "blocked",
                )
            )
            s.commit()


async def test_toggle_favourite_idempotent(seeded_db, session_factory):
    """toggle_favourite flips then flips back (idempotent after two toggles)."""
    with session_factory() as s:
        nid = _sql_scalar(seeded_db, "SELECT id FROM novel ORDER BY id LIMIT 1")
        repo = _repo(s)
        orig = (await repo.get_by_id(nid)).is_favourite
        await repo.toggle_favourite(nid)
        s.commit()
        v1 = (await repo.get_by_id(nid)).is_favourite
        assert v1 != orig
        await repo.toggle_favourite(nid)
        s.commit()
        v2 = (await repo.get_by_id(nid)).is_favourite
        assert v2 == orig


async def test_add_remove_tags_reference_count(seeded_db, session_factory):
    """add_tags/remove_tags keep novel.tags unique and reference_count exact."""
    tag_name = "SMOKETEST_TAG"
    with session_factory() as s:
        nid = _sql_scalar(seeded_db, "SELECT id FROM novel ORDER BY id LIMIT 1")
        repo = _repo(s)
        added = await repo.add_tags_to_novels([nid], {tag_name})
        s.commit()
        assert added == 1
        tags = (await repo.get_by_id(nid)).tags
        assert tag_name in tags
        assert _sql_scalar(
            seeded_db, "SELECT reference_count FROM tag WHERE name=:n", {"n": tag_name}
        ) == 1

        removed = await repo.remove_tags_from_novels([nid], {tag_name})
        s.commit()
        assert removed == 1
        tags2 = (await repo.get_by_id(nid)).tags
        assert tag_name not in tags2
        assert _sql_scalar(
            seeded_db, "SELECT reference_count FROM tag WHERE name=:n", {"n": tag_name}
        ) == 0
        # Cleanup the orphan tag row (reference_count 0).
        s.execute(_delete(Tag).where(Tag.name == tag_name))
        s.commit()


async def test_delete_many_cascade(seeded_db, session_factory):
    """delete_many drops the rows, the failure ledger, and decrements refs."""
    tag_name = "SMOKE_CASCADE_TAG"
    author_id = 999_999_991
    new_ids = [190_000_001, 190_000_002]
    with session_factory() as s:
        SQLAlchemyAuthorRepository(s).ensure_exists({author_id})
        for i, nid in enumerate(new_ids):
            s.add(Novel(
                id=nid, title=f"cascade{i}", author_id=author_id,
                tags=[tag_name], shuffle=1, like=100, text=100,
            ))
        s.flush()
        for nid in new_ids:
            s.add(FailedNovel(
                novel_id=nid, failure_type="x", error_message="e",
                failed_times=1, last_failed_at=datetime.now(timezone.utc),
            ))
        s.commit()

        assert _sql_scalar(
            seeded_db,
            "SELECT count(*) FROM failed_novel WHERE novel_id = ANY(:ids)",
            {"ids": new_ids},
        ) == 2

        paths = await _repo(s).delete_many(new_ids)
        s.commit()
        assert paths == []

        assert _sql_scalar(
            seeded_db,
            "SELECT count(*) FROM novel WHERE id = ANY(:ids)",
            {"ids": new_ids},
        ) == 0
        assert _sql_scalar(
            seeded_db,
            "SELECT count(*) FROM failed_novel WHERE novel_id = ANY(:ids)",
            {"ids": new_ids},
        ) == 0
        assert _sql_scalar(
            seeded_db, "SELECT reference_count FROM tag WHERE name=:n", {"n": tag_name}
        ) == 0
        # Cleanup the placeholder author + orphan tag row.
        s.execute(_delete(Author).where(Author.author_id == author_id))
        s.execute(_delete(Tag).where(Tag.name == tag_name))
        s.commit()


async def test_sort_by_ids_match_ids(seeded_db, session_factory):
    """sort_novel_ids / get_novels_by_ids / filter_ids_in_scope behave correctly."""
    with session_factory() as s:
        repo = _repo(s)
        ids = [r[0] for r in _sql(
            seeded_db, "SELECT id FROM novel WHERE \"like\" >= 500 ORDER BY id LIMIT 6"
        )]
        assert len(ids) >= 3

        sorted_ids = await repo.sort_novel_ids(ids, "like", "DESC")
        likes = [
            _sql_scalar(seeded_db, "SELECT \"like\" FROM novel WHERE id=:i", {"i": i})
            for i in sorted_ids
        ]
        assert likes == sorted(likes, reverse=True)

        by_ids = await repo.get_novels_by_ids(ids)
        assert [n.id for n in by_ids] == ids, "get_novels_by_ids must preserve order"

        in_scope = await repo.filter_ids_in_scope(ids, QuerySpec(min_like=1000))
        expected = [
            i for i in ids
            if _sql_scalar(seeded_db, "SELECT \"like\" FROM novel WHERE id=:i", {"i": i}) >= 1000
        ]
        assert sorted(in_scope) == sorted(expected)
