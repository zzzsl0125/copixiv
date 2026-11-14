"""PG vs Rust parity and post-commit delta sync tests against live Postgres."""

from __future__ import annotations

from datetime import datetime, timezone

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.core.services import EXCLUDE_BLOCKED_SETTING_KEY, QuerySpec
from copixiv.db.delta import SESSION_ENGINE_KEY
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, SQLAlchemySeriesRepository
from copixiv.features.novels.repo.read_repo import _novels_cards_match
from copixiv.features.system.repo import SQLAlchemySettingRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.query_engine.sync import attach_and_start_engine


def _seed_dataset(session_factory) -> None:
    now = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
    drafts = [
        NovelDraft(
            id=101,
            title="Alpha Ñ Story",
            author_id=10,
            author_name="AuthorTen",
            path="p/101.txt",
            text=4000,
            like=500,
            view=2000,
            series_id=1,
            series_name="SeriesOne",
            series_index=2,
            create_time=now,
            tags=["fantasy", "原神"],
        ),
        NovelDraft(
            id=102,
            title="Beta Tale",
            author_id=10,
            author_name="AuthorTen",
            path="p/102.txt",
            text=1500,
            like=200,
            view=900,
            series_id=1,
            series_name="SeriesOne",
            series_index=1,
            create_time=None,
            tags=["blocked_tag", "scifi"],
        ),
        NovelDraft(
            id=-150,
            title="Local Upload",
            author_id=20,
            author_name="AuthorTwenty",
            path="p/neg150.txt",
            text=6000,
            like=500,
            view=3000,
            series_id=None,
            series_name=None,
            series_index=None,
            create_time=now,
            tags=["local_tag"],
        ),
        NovelDraft(
            id=200,
            title="Gamma Blocked",
            author_id=20,
            author_name="AuthorTwenty",
            path="p/200.txt",
            text=8000,
            like=900,
            view=5000,
            create_time=None,
            tags=["blocked_tag", "原神"],
        ),
    ]
    from copixiv.db import models

    uow = SqlUnitOfWork(session_factory)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).upsert_novels(drafts)
        local_row = uow.session.get(models.Novel, -150)
        assert local_row is not None
        local_row.source = "local"
        SQLAlchemyTagRepository(uow.session).create_preference(
            {"tag": "blocked_tag", "preference": "blocked", "sort_index": 0},
        )
        SQLAlchemySettingRepository(uow.session).set_value(
            EXCLUDE_BLOCKED_SETTING_KEY, "true",
        )


def _assert_pg_rust_parity(session_factory, engine, specs: list[QuerySpec]) -> None:
    with session_factory() as session:
        pg_repo = SQLAlchemyNovelRepository(session)
        rs_repo = SQLAlchemyNovelRepository(session, query_engine=engine)
        for spec in specs:
            pg_page = pg_repo.get_novels(spec)
            rs_page = rs_repo.get_novels(spec)
            assert [n.id for n in pg_page["novels"]] == [
                n.id for n in rs_page["novels"]
            ], f"get_novels mismatch for {spec}"
            assert _novels_cards_match(
                pg_page["novels"], rs_page["novels"],
            ), f"card fields mismatch for {spec}"
            assert (pg_page["cursor"] is not None) == (
                rs_page["cursor"] is not None
            ), f"cursor presence mismatch for {spec}"
            if pg_page["cursor"] is not None:
                s2 = spec.model_copy(update={"cursor": pg_page["cursor"]})
                pg_p2 = pg_repo.get_novels(s2)["novels"]
                rs_p2 = rs_repo.get_novels(s2)["novels"]
                assert [n.id for n in pg_p2] == [
                    n.id for n in rs_p2
                ], f"page 2 mismatch for {spec}"
                assert _novels_cards_match(
                    pg_p2, rs_p2,
                ), f"page 2 card fields mismatch for {spec}"

            assert pg_repo.count_novels(spec) == rs_repo.count_novels(spec)
            assert pg_repo.count_excluded_novels(spec) == rs_repo.count_excluded_novels(spec)
            assert pg_repo.has_excluded_novels(spec) == rs_repo.has_excluded_novels(spec)
            assert pg_repo.list_matching_ids(spec) == rs_repo.list_matching_ids(spec)
            assert pg_repo.filter_ids_in_scope(
                [-150, 101, 102, 200, 999999], spec,
            ) == rs_repo.filter_ids_in_scope([-150, 101, 102, 200, 999999], spec)

        pg_cards = pg_repo.get_novel_cards_by_ids([200, -150, 101, 999999])
        rs_cards = rs_repo.get_novel_cards_by_ids([200, -150, 101, 999999])
        assert _novels_cards_match(pg_cards, rs_cards), "get_novel_cards_by_ids mismatch"

        assert set(pg_repo.list_blocked_ids()) == set(rs_repo.list_blocked_ids())
        for ob in ("id", "like", "view", "text", "create_time"):
            for d in ("DESC", "ASC"):
                ids = [200, 102, 101, -150, 999]
                assert pg_repo.sort_novel_ids(ids, order_by=ob, order_direction=d) == (
                    rs_repo.sort_novel_ids(ids, order_by=ob, order_direction=d)
                )


def test_parity_across_filters_sorts_nulls_and_shadow_mode(
    db_session_factory, caplog,
):
    _seed_dataset(db_session_factory)
    store = PgSnapshotStore(db_session_factory)
    engine = attach_and_start_engine(store, mode="shadow", build_initial=True)
    try:
        specs = [
            QuerySpec(order_by="id", order_direction="DESC", per_page=2),
            QuerySpec(order_by="id", order_direction="ASC", per_page=2),
            QuerySpec(order_by="like", order_direction="DESC", per_page=2),
            QuerySpec(order_by="create_time", order_direction="DESC", per_page=1),
            QuerySpec(order_by="create_time", order_direction="ASC", per_page=2),
            QuerySpec(order_by="title", order_direction="ASC", per_page=2),
            QuerySpec(order_by="author_name", order_direction="DESC", per_page=2),
            QuerySpec(order_by="series_name", order_direction="ASC", per_page=2),
            QuerySpec(conditions=[("keyword", "Alpha Ñ")], order_by="id"),
            QuerySpec(conditions=[("keyword", "原")], order_by="id"),
            QuerySpec(conditions=[("tags", "原神")], order_by="like"),
            QuerySpec(conditions=[("source", "local")], order_by="id"),
            QuerySpec(min_like=300, min_text=2000, exclude_blocked_tags=False),
            QuerySpec(exclude_ids=[101]),
        ]

        # Run in shadow mode directly through UoW and verify zero parity warnings.
        caplog.clear()
        with db_session_factory() as s:
            shadow_repo = SQLAlchemyNovelRepository(s, query_engine=engine)
            for sp in specs:
                shadow_repo.get_novels(sp)
                shadow_repo.count_novels(sp)
                shadow_repo.count_excluded_novels(sp)
                shadow_repo.has_excluded_novels(sp)
                shadow_repo.list_matching_ids(sp)
                shadow_repo.filter_ids_in_scope([101, 102, -150, 200], sp)
            shadow_repo.list_blocked_ids()
            shadow_repo.sort_novel_ids([101, -150, 200], order_by="create_time")
            shadow_repo.get_novel_cards_by_ids([200, -150, 101, 999999])
        assert "Shadow parity mismatch" not in caplog.text
        assert "Shadow card-field mismatch" not in caplog.text

        # Now switch to pure rust mode and assert exact equality with PG on all specs.
        engine._mode = "rust"
        _assert_pg_rust_parity(db_session_factory, engine, specs)
    finally:
        store.detach_runtime()


def test_delta_sync_all_write_paths_and_rollback_isolation(db_session_factory):
    _seed_dataset(db_session_factory)
    store = PgSnapshotStore(db_session_factory)
    engine = attach_and_start_engine(store, mode="rust", build_initial=True)
    probe_specs = [
        QuerySpec(order_by="id", per_page=10),
        QuerySpec(order_by="like", per_page=10),
        QuerySpec(order_by="series_index", per_page=10),
        QuerySpec(conditions=[("is_favourite", "1")]),
        QuerySpec(conditions=[("is_special_follow", "true")]),
        QuerySpec(conditions=[("tags", "canonical_tag")]),
        QuerySpec(conditions=[("keyword", "RenamedAuthor")]),
        QuerySpec(exclude_blocked_tags=None),
    ]
    try:
        # 1. toggle_favourite + toggle_special_follow + update_has_epub_status
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            nrepo = SQLAlchemyNovelRepository(uow.session)
            assert uow.session.info.get(SESSION_ENGINE_KEY) is engine
            nrepo.toggle_favourite(101)
            nrepo.toggle_special_follow(10, )
            nrepo.update_has_epub_status([101], EpubStatus.DONE)
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 2. update_metadata (ORM) + update_series_indices (Core SQL)
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            SQLAlchemyNovelRepository(uow.session).update_metadata(
                101, {"title": "Edited Title", "like": 1234, "tags": ["fantasy", "edited"]},
            )
            SQLAlchemySeriesRepository(uow.session).update_series_indices({101: 9})
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 3. add_tags_to_novels + remove_tags_from_novels + update_metadata_many
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            nrepo = SQLAlchemyNovelRepository(uow.session)
            nrepo.add_tags_to_novels([101, -150], {"batch_tag", "legacy_alias_src"})
            nrepo.remove_tags_from_novels([101], {"fantasy"})
            nrepo.update_metadata_many([-150], {"series_id": 1, "series_name": "SeriesOne"})
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 4. update_author_name + apply_alias_retroactively
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            SQLAlchemyAuthorRepository(uow.session).update_author_name(10, "RenamedAuthor")
            SQLAlchemyTagRepository(uow.session).apply_alias_retroactively(
                "legacy_alias_src", "canonical_tag",
            )
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 5. System setting & tag preference toggles
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            SQLAlchemySettingRepository(uow.session).set_value(
                EXCLUDE_BLOCKED_SETTING_KEY, "false",
            )
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            prefs = SQLAlchemyTagRepository(uow.session).get_preferences()
            for p in prefs:
                SQLAlchemyTagRepository(uow.session).delete_preference(p.id)
            SQLAlchemySettingRepository(uow.session).set_value(
                EXCLUDE_BLOCKED_SETTING_KEY, "true",
            )
            SQLAlchemyTagRepository(uow.session).create_preference(
                {"tag": "canonical_tag", "preference": "blocked", "sort_index": 0},
            )
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 6. Rollback isolation: mutate ORM & Core inside failed transaction
        with db_session_factory() as s:
            empty_q = SQLAlchemyNovelRepository(s)._compile_engine_query(QuerySpec())
        before_count = engine.count_novels(empty_q, cap=5000).count
        try:
            uow = SqlUnitOfWork(db_session_factory)
            with uow.begin():
                SQLAlchemyNovelRepository(uow.session).upsert_novels(
                    [NovelDraft(id=999, title="RolledBack", path="p/999.txt", author_id=10)],
                )
                SQLAlchemyNovelRepository(uow.session).delete_many([101])
                raise RuntimeError("abort transaction")
        except RuntimeError:
            pass
        assert engine.count_novels(empty_q, cap=5000).count == before_count
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)

        # 7. delete (ORM) + delete_many (Core SQL) + delete_author_and_data
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            SQLAlchemyNovelRepository(uow.session).delete(101)
            SQLAlchemyAuthorRepository(uow.session).delete_author_and_data(20)
        uow = SqlUnitOfWork(db_session_factory)
        with uow.begin():
            SQLAlchemyNovelRepository(uow.session).delete_many([102])
        _assert_pg_rust_parity(db_session_factory, engine, probe_specs)
    finally:
        store.detach_runtime()
