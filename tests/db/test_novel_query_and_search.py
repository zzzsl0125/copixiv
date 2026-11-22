"""Layer 2 DB tests: Rust keyword search, tag filters, 400 validation, and mixed-ID cursor pagination."""

from __future__ import annotations

import pytest

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.core.models import EpubStatus
from copixiv.core.search import QuerySpec
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


def _seed_search_corpus(uow: SqlUnitOfWork) -> None:
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=5000,
                    title="星辰之海 Chinese",
                    author_id=1,
                    author_name="Alice",
                    series_id=10,
                    series_name="银河物语",
                    like=500,
                    view=2000,
                    text=5000,
                    tags=["百合", "校园", "r-18"],
                ),
                build_novel(
                    id=3000,
                    title="暗夜行者",
                    author_id=2,
                    author_name="Bob",
                    series_id=None,
                    series_name=None,
                    like=200,
                    view=1000,
                    text=4000,
                    tags=["百合", "ntr"],
                ),
                build_novel(
                    id=1000,
                    title="日常随笔",
                    author_id=1,
                    author_name="Alice",
                    series_id=None,
                    series_name=None,
                    like=100,
                    view=500,
                    text=3000,
                    tags=["日常"],
                ),
            ],
        )
        # Insert two local (negative-ID) novels interleaved on abs(id) timeline: abs=4000 and abs=2000
        for neg_id, title in [(-4000, "本地星辰"), (-2000, "本地旧作")]:
            uow.session.add(
                models.Novel(
                    id=neg_id,
                    title=title,
                    author_id=1,
                    author_name="Alice",
                    path=f"download/local/{title}_{neg_id}.txt",
                    like=150,
                    view=600,
                    text=3500,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=["百合"],
                    source="local",
                    source_key=f"sha_{neg_id}",
                )
            )
        uow.session.add(models.TagPreference(tag="ntr", preference=models.TagPreferenceORM.blocked))


def test_keyword_search_and_tag_predicates(uow: SqlUnitOfWork):
    _seed_search_corpus(uow)

    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)

        # CJK substring + ASCII case-insensitive search via Rust query engine
        res_upper = repo.get_novels(QuerySpec(conditions=[("keyword", "星辰 Chinese")]))
        res_lower = repo.get_novels(QuerySpec(conditions=[("keyword", "星辰 chinese")]))
        assert [n.id for n in res_upper["novels"]] == [5000]
        assert [n.id for n in res_lower["novels"]] == [5000]

        # Single CJK char across title / series_name / author_name / tags
        res_single = repo.get_novels(QuerySpec(conditions=[("keyword", "海")]))
        assert [n.id for n in res_single["novels"]] == [5000]

        # Tag AND filter (@>) + automatic blocked-tag exclusion (NOT &&)
        res_tag = repo.get_novels(QuerySpec(conditions=[("tags", "百合")]))
        ids_tag = {n.id for n in res_tag["novels"]}
        assert 3000 not in ids_tag  # excluded because it carries blocked tag "ntr"
        assert {5000, -4000, -2000} == ids_tag
        assert repo.has_excluded_novels(QuerySpec(conditions=[("tags", "百合")])) is True
        res_blocked = repo.get_novels(QuerySpec(conditions=[("tags", "百合")]), only_blocked=True)
        assert [n.id for n in res_blocked["novels"]] == [3000]

        # Explicit override shows blocked novel
        res_all = repo.get_novels(QuerySpec(conditions=[("tags", "百合")], exclude_blocked_tags=False))
        assert {n.id for n in res_all["novels"]} == {5000, 3000, -4000, -2000}


@pytest.mark.parametrize(
    "spec",
    [
        QuerySpec(conditions=[("id", "abc")]),
        QuerySpec(conditions=[("author_id", "xyz")]),
        QuerySpec(conditions=[("series_id", "bad")]),
        QuerySpec(conditions=[("nonexistent_col", "1")]),
        QuerySpec(order_by="id", cursor={"id": "not_an_int"}),
        QuerySpec(order_by="like", cursor={"id": 1}),
    ],
)
def test_query_builder_raises_validation_error_not_db_error(uow: SqlUnitOfWork, spec: QuerySpec):
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        with pytest.raises(ValidationError):
            repo.get_novels(spec)


def test_mixed_positive_negative_id_cursor_pagination(uow: SqlUnitOfWork):
    _seed_search_corpus(uow)

    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        collected: list[int] = []
        cursor = None
        while True:
            page = repo.get_novels(
                QuerySpec(
                    order_by="id",
                    order_direction="DESC",
                    per_page=2,
                    cursor=cursor,
                    exclude_blocked_tags=False,
                )
            )
            collected.extend(n.id for n in page["novels"])
            cursor = page["cursor"]
            if cursor is None:
                break

        # Strict abs(id) DESC order across Pixiv (+) and local (-) rows, zero drops/dupes
        assert collected == [5000, -4000, 3000, -2000, 1000]
