"""DB-truth oracle parity, SQLAlchemy event-sync, savepoint, concurrency, and failover tests."""

from __future__ import annotations

import functools
import importlib.util
import threading
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.core.search import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    QuerySpec,
    normalize_search_segments,
)
from copixiv.db import models
from copixiv.db.delta import SESSION_ENGINE_KEY, detach_engine_runtime, get_attached_engine
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.system.repo import SQLAlchemySettingRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.query_engine.sync import attach_and_start_engine


def _ascii_lower(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def _seed_dataset(session_factory) -> None:
    now = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
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
            EXCLUDE_BLOCKED_SETTING_KEY,
            "true",
        )


def _load_db_truth(session) -> tuple[list[dict[str, Any]], set[str], bool]:
    sf_authors = set(
        session.execute(
            select(models.Author.author_id).where(models.Author.is_special_follow == True)  # noqa: E712
        )
        .scalars()
        .all()
    )
    blocked = set(
        session.execute(
            select(models.TagPreference.tag).where(
                models.TagPreference.preference == models.TagPreferenceORM.blocked
            )
        )
        .scalars()
        .all()
    )
    setting = session.execute(
        select(models.Setting.value).where(models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY)
    ).scalar_one_or_none()
    exclude_default = setting != "false" if setting is not None else True

    novels = session.execute(select(models.Novel)).scalars().all()
    rows: list[dict[str, Any]] = []
    for n in novels:
        rows.append(
            {
                "id": int(n.id),
                "title": n.title,
                "author_id": n.author_id,
                "author_name": n.author_name,
                "series_id": n.series_id,
                "series_name": n.series_name,
                "series_index": n.series_index,
                "text": int(n.text),
                "like": int(n.like),
                "view": int(n.view),
                "has_epub": int(n.has_epub),
                "create_time": n.create_time,
                "source": n.source,
                "is_favourite": bool(n.is_favourite),
                "is_special_follow": (n.author_id in sf_authors) if n.author_id is not None else False,
                "tags": list(n.tags or []),
            }
        )
    return rows, blocked, exclude_default


def _oracle_filter(
    rows: list[dict[str, Any]],
    blocked: set[str],
    exclude_default: bool,
    spec: QuerySpec,
    *,
    count_blocked: bool = False,
    restrict_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    exclude_active = spec.exclude_blocked_tags if spec.exclude_blocked_tags is not None else exclude_default
    out: list[dict[str, Any]] = []
    for row in rows:
        nid = row["id"]
        if restrict_ids is not None and nid not in restrict_ids:
            continue
        if spec.exclude_ids and nid in spec.exclude_ids:
            continue
        if spec.min_like is not None and row["like"] < spec.min_like:
            continue
        if spec.min_text is not None and row["text"] < spec.min_text:
            continue

        skip = False
        for k, v in spec.conditions:
            if not isinstance(v, str) or not v.strip():
                continue
            if k == "tags":
                if v not in row["tags"]:
                    skip = True
                    break
            elif k == "keyword":
                joined = _ascii_lower(
                    " ".join(
                        p
                        for p in (
                            row["title"],
                            row["author_name"],
                            row["series_name"],
                            " ".join(row["tags"]),
                        )
                        if p
                    )
                )
                if not all(_ascii_lower(seg) in joined for seg in normalize_search_segments(v)):
                    skip = True
                    break
            elif k == "is_favourite":
                if not row["is_favourite"]:
                    skip = True
                    break
            elif k == "is_special_follow":
                if not row["is_special_follow"]:
                    skip = True
                    break
            elif k in ("id", "author_id", "series_id", "series_index", "like", "view", "text", "has_epub"):
                if row[k] != int(v):
                    skip = True
                    break
            else:
                if row[k] != v:
                    skip = True
                    break
        if skip:
            continue

        has_blocked = bool(set(row["tags"]) & blocked)
        if count_blocked:
            if not (exclude_active and has_blocked):
                continue
        elif exclude_active and has_blocked:
            continue
        out.append(row)
    return out


def _oracle_sort_rows(
    rows: list[dict[str, Any]], order_by: str, order_direction: str
) -> list[dict[str, Any]]:
    desc = order_direction.upper() == "DESC"
    if order_by == "id":
        return sorted(rows, key=lambda r: (abs(r["id"]), r["id"]), reverse=desc)

    def _cmp(a: dict[str, Any], b: dict[str, Any]) -> int:
        va = a[order_by]
        vb = b[order_by]
        if (va is None) != (vb is None):
            if desc:
                return -1 if va is None else 1
            return 1 if va is None else -1
        if va is not None and va != vb:
            if desc:
                return -1 if va > vb else 1
            return -1 if va < vb else 1
        return -1 if ((a["id"] > b["id"]) if desc else (a["id"] < b["id"])) else 1

    return sorted(rows, key=functools.cmp_to_key(_cmp))


def _assert_db_oracle_parity(session_factory, engine, specs: list[QuerySpec]) -> None:
    with session_factory() as session:
        rows, blocked, exclude_default = _load_db_truth(session)
        rs_repo = SQLAlchemyNovelRepository(session, query_engine=engine)
        card_fields = (
            "id",
            "title",
            "author_id",
            "author_name",
            "series_id",
            "series_name",
            "series_index",
            "text",
            "like",
            "view",
            "has_epub",
            "create_time",
            "source",
            "is_favourite",
            "tags",
            "is_special_follow",
        )
        by_id = {r["id"]: r for r in rows}

        def _assert_cards_equal(novels: list[Any], context: str) -> None:
            for n in novels:
                truth = by_id[n.id]
                for f in card_fields:
                    got = getattr(n, f)
                    exp = truth[f]
                    if f == "has_epub":
                        got = int(got)
                    assert got == exp, f"{context} field {f} mismatch on novel {n.id}: {got!r} != {exp!r}"

        for spec in specs:
            expected_vis = _oracle_filter(rows, blocked, exclude_default, spec)
            expected_blk = _oracle_filter(rows, blocked, exclude_default, spec, count_blocked=True)

            if spec.order_by not in ("random", "none"):
                ordered = _oracle_sort_rows(expected_vis, spec.order_by or "id", spec.order_direction)
                p1 = rs_repo.get_novels(spec)
                exp_p1 = ordered[: spec.per_page]
                assert [n.id for n in p1["novels"]] == [r["id"] for r in exp_p1], f"page1 mismatch for {spec}"
                _assert_cards_equal(p1["novels"], f"page1 {spec}")
                assert (p1["cursor"] is not None) == (len(ordered) > spec.per_page)
                if p1["cursor"] is not None:
                    last_val = p1["cursor"].get(spec.order_by or "id")
                    if spec.order_by != "create_time" or last_val is not None:
                        p2 = rs_repo.get_novels(spec.model_copy(update={"cursor": p1["cursor"]}))
                        exp_p2 = ordered[spec.per_page : spec.per_page * 2]
                        assert [n.id for n in p2["novels"]] == [r["id"] for r in exp_p2], (
                            f"page2 mismatch for {spec}"
                        )
                        _assert_cards_equal(p2["novels"], f"page2 {spec}")

            assert rs_repo.count_novels(spec).value == len(expected_vis)
            assert rs_repo.count_excluded_novels(spec).value == len(expected_blk)
            assert rs_repo.has_excluded_novels(spec) is (len(expected_blk) > 0)
            assert rs_repo.list_matching_ids(spec) == sorted(r["id"] for r in expected_vis)

            scope = [-150, 101, 102, 200, 999999]
            expected_scope = _oracle_filter(rows, blocked, exclude_default, spec, restrict_ids=scope)
            assert rs_repo.filter_ids_in_scope(scope, spec) == sorted(r["id"] for r in expected_scope)

        req_ids = [r["id"] for r in rows[:3]] + [999999]
        cards = rs_repo.get_novel_cards_by_ids(req_ids)
        assert [c.id for c in cards] == [nid for nid in req_ids if nid in by_id]
        _assert_cards_equal(cards, "get_novel_cards_by_ids")

        exp_blocked_ids = {r["id"] for r in rows if exclude_default and (set(r["tags"]) & blocked)}
        assert set(rs_repo.list_blocked_ids()) == exp_blocked_ids

        all_ids_desc_arg = [r["id"] for r in rows] + [999999]
        present_rows = list(rows)
        for ob in ("id", "like", "view", "text", "create_time"):
            for d in ("DESC", "ASC"):
                exp_sorted = [r["id"] for r in _oracle_sort_rows(present_rows, ob, d)]
                assert rs_repo.sort_novel_ids(all_ids_desc_arg, order_by=ob, order_direction=d) == exp_sorted


def test_parity_across_filters_sorts_and_nulls(db_session_factory):
    _seed_dataset(db_session_factory)
    engine = get_attached_engine(db_session_factory)
    assert engine is not None
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
    _assert_db_oracle_parity(db_session_factory, engine, specs)


def test_delta_sync_all_write_paths_and_rollback_isolation(db_session_factory):
    _seed_dataset(db_session_factory)
    engine = get_attached_engine(db_session_factory)
    assert engine is not None
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

    # 1. toggle_favourite + toggle_special_follow + update_has_epub_status
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        nrepo = SQLAlchemyNovelRepository(uow.session)
        assert uow.session.info.get(SESSION_ENGINE_KEY) is engine
        nrepo.toggle_favourite(101)
        SQLAlchemyAuthorRepository(uow.session).toggle_special_follow(10)
        nrepo.update_has_epub_status([101], EpubStatus.DONE)
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    # 2. update_metadata (ORM) + update_series_indices (Core SQL)
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).update_metadata(
            101,
            {"title": "Edited Title", "like": 1234, "tags": ["fantasy", "edited"]},
        )
        SQLAlchemySeriesRepository(uow.session).update_series_indices({101: 9})
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    # 3. add_tags_to_novels + remove_tags_from_novels + update_metadata_many
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        nrepo = SQLAlchemyNovelRepository(uow.session)
        nrepo.add_tags_to_novels([101, -150], {"batch_tag", "legacy_alias_src"})
        nrepo.remove_tags_from_novels([101], {"fantasy"})
        nrepo.update_metadata_many([-150], {"series_id": 1, "series_name": "SeriesOne"})
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    # 4. update_author_name + apply_alias_retroactively
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        SQLAlchemyAuthorRepository(uow.session).update_author_name(10, "RenamedAuthor")
        SQLAlchemyTagRepository(uow.session).apply_alias_retroactively(
            "legacy_alias_src",
            "canonical_tag",
        )
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    # 5. System setting & tag preference toggles
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        SQLAlchemySettingRepository(uow.session).set_value(
            EXCLUDE_BLOCKED_SETTING_KEY,
            "false",
        )
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        prefs = SQLAlchemyTagRepository(uow.session).get_preferences()
        for p in prefs:
            SQLAlchemyTagRepository(uow.session).delete_preference(p.id)
        SQLAlchemySettingRepository(uow.session).set_value(
            EXCLUDE_BLOCKED_SETTING_KEY,
            "true",
        )
        SQLAlchemyTagRepository(uow.session).create_preference(
            {"tag": "canonical_tag", "preference": "blocked", "sort_index": 0},
        )
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

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
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)

    # 7. delete (ORM) + delete_many (Core SQL) + delete_author_and_data
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete(101)
        SQLAlchemyAuthorRepository(uow.session).delete_author_and_data(20)
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete_many([102])
    _assert_db_oracle_parity(db_session_factory, engine, probe_specs)


def test_raw_session_bypass_uow_and_savepoint_isolation(committed_session_factory):
    """Raw ``session_factory()`` commits & nested savepoints sync accurately without UoW."""
    _seed_dataset(committed_session_factory)
    engine = get_attached_engine(committed_session_factory)
    assert engine is not None

    # Direct raw sessionmaker ORM write (no SqlUnitOfWork) syncs via after_flush/after_commit/after_transaction_end
    with committed_session_factory() as s, s.begin():
        s.add(
            models.Novel(
                id=501,
                title="DirectRawORM",
                author_id=10,
                author_name="AuthorTen",
                like=777,
                tags=["raw_tag"],
            )
        )
    with committed_session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        assert [
            n.id for n in repo.get_novels(QuerySpec(conditions=[("keyword", "DirectRawORM")]))["novels"]
        ] == [501]

    # Scenario A: outer commits while inner begin_nested() savepoint rolls back -> phantom write NOT synced
    with committed_session_factory() as s, s.begin():
        s.add(
            models.Novel(id=502, title="OuterKept", author_id=10, author_name="AuthorTen", tags=["kept_tag"])
        )
        with suppress(ValueError), s.begin_nested():
            s.add(models.Novel(id=503, title="InnerSavepointAborted", author_id=10, tags=["aborted_sp_tag"]))
            s.flush()
            raise ValueError("savepoint failure")
    _assert_db_oracle_parity(
        committed_session_factory,
        engine,
        [QuerySpec(order_by="id"), QuerySpec(conditions=[("keyword", "InnerSavepointAborted")])],
    )

    # Scenario B: inner savepoint commits, outer rolls back -> nothing synced
    with committed_session_factory() as s, suppress(ValueError), s.begin():
        with s.begin_nested():
            s.add(models.Novel(id=504, title="InnerCommitOuterAbort", author_id=10))
        raise ValueError("outer failure")
    with committed_session_factory() as s:
        assert (
            SQLAlchemyNovelRepository(s).list_matching_ids(
                QuerySpec(conditions=[("keyword", "InnerCommitOuterAbort")])
            )
            == []
        )


def test_pool_size_one_no_deadlock_on_after_transaction_end(pg_test_engine, committed_session_factory):
    """Ensure ``pool_size=1, max_overflow=0`` does NOT deadlock when post-commit opens snapshot session."""
    tiny_engine = create_engine(
        pg_test_engine.url,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3.0,
    )
    sf = sessionmaker(bind=tiny_engine, autocommit=False, autoflush=False, expire_on_commit=True)
    store = PgSnapshotStore(sf)
    eng = attach_and_start_engine(store, build_initial=True)
    try:
        with sf() as s, s.begin():
            s.add(models.Novel(id=801, title="PoolOneCheck", like=42, tags=["pool1"]))
        with sf() as s:
            hits = SQLAlchemyNovelRepository(s, query_engine=eng).list_matching_ids(
                QuerySpec(conditions=[("tags", "pool1")])
            )
            assert hits == [801]
    finally:
        detach_engine_runtime(sf)
        tiny_engine.dispose()


def test_concurrent_readers_writers_and_self_healing_rebuild(committed_session_factory, monkeypatch):
    """Multi-thread read/write stress + failed post-commit sync auto-recovery via needs_rebuild."""
    _seed_dataset(committed_session_factory)
    engine = get_attached_engine(committed_session_factory)
    assert engine is not None
    store = PgSnapshotStore(committed_session_factory)
    errors: list[BaseException] = []

    def _writer(worker_idx: int) -> None:
        try:
            for j in range(6):
                nid = 2000 + worker_idx * 100 + j
                uow = SqlUnitOfWork(committed_session_factory)
                with uow.begin():
                    SQLAlchemyNovelRepository(uow.session).upsert_novels(
                        [
                            NovelDraft(
                                id=nid,
                                title=f"Conc_{worker_idx}_{j}",
                                path=f"p/{nid}.txt",
                                author_id=10,
                                author_name="AuthorTen",
                                like=nid,
                                tags=[f"ctag_{j % 3}"],
                            )
                        ]
                    )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def _reader() -> None:
        try:
            for _ in range(15):
                with committed_session_factory() as s:
                    repo = SQLAlchemyNovelRepository(s, query_engine=engine)
                    repo.get_novels(QuerySpec(order_by="like", per_page=5))
                    repo.count_novels(QuerySpec(min_like=100))
                    SQLAlchemyTagRepository(s, query_engine=engine).suggest_aliases(limit=3)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(0,)),
        threading.Thread(target=_writer, args=(1,)),
        threading.Thread(target=_reader),
        threading.Thread(target=_reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"Concurrent stress errors: {errors!r}"
    _assert_db_oracle_parity(
        committed_session_factory,
        engine,
        [QuerySpec(order_by="id"), QuerySpec(conditions=[("tags", "ctag_1")])],
    )

    # Simulate transient failure in post-commit delta sync -> mark_needs_rebuild + auto heal
    store_module = importlib.util.find_spec("copixiv.query_engine.sync")
    assert store_module is not None
    detach_engine_runtime(committed_session_factory)
    eng2 = attach_and_start_engine(store, build_initial=True)

    orig_fetch = PgSnapshotStore.fetch_delta_payload
    fail_once = {"armed": True}

    def _flaky_fetch(self, pending):
        if fail_once["armed"]:
            fail_once["armed"] = False
            raise RuntimeError("simulated DB snapshot hiccup")
        return orig_fetch(self, pending)

    monkeypatch.setattr(PgSnapshotStore, "fetch_delta_payload", _flaky_fetch)
    # Fail BOTH delta sync and immediate fallback once by raising from stream_snapshot on first fail
    orig_stream = PgSnapshotStore.stream_snapshot
    fail_rebuild_once = {"armed": True}

    def _flaky_stream(self, consume_total, consume_batch, batch_size=2_000):
        if fail_rebuild_once["armed"]:
            fail_rebuild_once["armed"] = False
            raise RuntimeError("simulated rebuild hiccup")
        return orig_stream(self, consume_total, consume_batch, batch_size=batch_size)

    monkeypatch.setattr(PgSnapshotStore, "stream_snapshot", _flaky_stream)
    with committed_session_factory() as s, s.begin():
        s.add(models.Novel(id=3001, title="NeedRebuildRow", author_id=10))
    assert eng2.needs_rebuild() is True

    # Next commit succeeds in full self-healing rebuild and clears needs_rebuild
    with committed_session_factory() as s, s.begin():
        s.add(models.Novel(id=3002, title="HealedRow", author_id=10))
    assert eng2.needs_rebuild() is False
    with committed_session_factory() as s:
        assert set(
            SQLAlchemyNovelRepository(s, query_engine=eng2).get_novel_cards_by_ids([3001, 3002])[i].id
            for i in range(2)
        ) == {3001, 3002}
