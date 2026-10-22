"""Repository integration tests (PostgreSQL-backed, post-migration).

Post-migration rewrite: the repository methods now operate on the PG
schema — ``novel.tags text[]`` (no ``novel_tag`` join table),
``novel.is_favourite`` boolean (no ``favourite`` table), a keyword-search
expression index on ``novel`` (no derived search table), and
``failed_novel``'s explicit cleanup.  The tests use the session-scoped
PG ``session_factory`` and a per-test truncation via ``clean_db``.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import (
    Novel, Author, Tag, TagAlias, TaskHistory, ScheduledTask, FailedNovel,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.tags import repo as tag_repo
from copixiv.features.tags.repo import SQLAlchemyTagRepository, _like_patterns


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class TestSQLAlchemyNovelRepository:
    def test_upsert_new_novel(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=10, author_name="Test Author"))
            s.commit()

        with session_factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            count = asyncio.run(repo.upsert_novels([{
                "id": 1, "title": "Test Novel", "author_id": 10,
                "path": "/tmp/n1.txt", "text": 5000,
                "tags": ["R-18", "NTR"],
            }]))
            s.commit()
            assert count == 1
            novel = s.get(Novel, 1)
            assert novel is not None
            assert novel.title == "Test Novel"
            assert set(novel.tags) == {"R-18", "NTR"}

    def test_upsert_novel_model_with_serializer_nulled(self, session_factory):
        """Regression (2026-08-19 cron failures): upsert must not depend on
        ``Novel.__pydantic_serializer__``."""
        from pydantic_core import SchemaSerializer
        from copixiv.core.models import Novel as NovelModel, EpubStatus

        with session_factory() as s:
            s.add(Author(author_id=10, author_name="Test Author"))
            s.commit()

        novel = NovelModel(
            id=1, title="带图的小说", author_id=10, path="/tmp/n1.txt",
            has_epub=EpubStatus.PENDING, tags=["R-18", "NTR"],
        )

        try:
            NovelModel.__pydantic_serializer__ = None
            with session_factory() as s:
                repo = SQLAlchemyNovelRepository(s)
                count = asyncio.run(repo.upsert_novels([novel]))
                s.commit()
        finally:
            NovelModel.model_rebuild(force=True)

        assert count == 1
        with session_factory() as s:
            row = s.get(Novel, 1)
            assert row is not None
            assert row.title == "带图的小说"

    def test_get_existing_ids(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="Test Author"))
            s.flush()
            s.add(Novel(id=1, title="A", author_id=1, path="/tmp/a.txt"))
            s.add(Novel(id=2, title="B", author_id=1, path="/tmp/b.txt"))
            s.commit()

        with session_factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            existing = asyncio.run(repo.get_existing_ids({1, 2, 3}))
            assert existing == {1, 2}

    def test_toggle_favourite(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="Test Author"))
            s.flush()
            s.add(Novel(id=1, title="F", author_id=1, path="/tmp/f.txt"))
            s.commit()

        with session_factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            asyncio.run(repo.toggle_favourite(1))
            s.commit()
            assert s.get(Novel, 1).is_favourite is True
            asyncio.run(repo.toggle_favourite(1))
            s.commit()
            assert s.get(Novel, 1).is_favourite is False


class TestSQLAlchemyAuthorRepository:
    def test_get_by_id(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=99, author_name="Test Author"))
            s.commit()
            repo = SQLAlchemyAuthorRepository(s)
            author = asyncio.run(repo.get_by_id(99))
            assert author is not None
            assert author["author_name"] == "Test Author"

    def test_need_update_new_author(self, session_factory):
        with session_factory() as s:
            repo = SQLAlchemyAuthorRepository(s)
            assert asyncio.run(repo.need_update(999)) is True

    def test_need_update_with_date(self, session_factory):
        with session_factory() as s:
            s.add(Author(
                author_id=5, author_name="Old",
                last_update=datetime.now(timezone.utc),
            ))
            s.commit()
            repo = SQLAlchemyAuthorRepository(s)
            assert asyncio.run(repo.need_update(5)) is False


class TestSQLAlchemyTagRepository:
    def test_get_alias_map_with_fk_tags(self, session_factory):
        with session_factory() as s:
            repo = SQLAlchemyTagRepository(s)
            ids = {}
            for name in ("R-18", "R18", "NTR", "ntr"):
                ids[name] = repo._get_or_create_tag_id(name)
            s.flush()
            s.add(TagAlias(source=ids["R-18"], target=ids["R18"]))
            s.add(TagAlias(source=ids["NTR"], target=ids["ntr"]))
            s.commit()
            alias_map = repo.get_alias_map_sync()
            assert alias_map.get("R-18") == "R18"
            assert alias_map.get("NTR") == "ntr"

    def test_create_alias_via_repository(self, session_factory):
        with session_factory() as s:
            repo = SQLAlchemyTagRepository(s)
            ids = {}
            for name in ("source_tag", "target_tag"):
                ids[name] = repo._get_or_create_tag_id(name)
            s.flush()
            result = asyncio.run(repo.create_alias({
                "source": "source_tag",
                "target": "target_tag",
            }))
            s.commit()
            assert result["source"] == "source_tag"
            assert result["target"] == "target_tag"
            alias = s.get(TagAlias, result["id"])
            assert alias.source == ids["source_tag"]
            assert alias.target == ids["target_tag"]

    def test_alias_fk_integrity(self, session_factory):
        with session_factory() as s:
            s.add(Tag(name="valid", reference_count=0))
            s.flush()
            s.add(TagAlias(source=1, target=999))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

    def test_unique_source_constraint(self, session_factory):
        with session_factory() as s:
            repo = SQLAlchemyTagRepository(s)
            ids = {n: repo._get_or_create_tag_id(n) for n in ("src", "tgt1", "tgt2")}
            s.flush()
            s.add(TagAlias(source=ids["src"], target=ids["tgt1"]))
            s.commit()
            s.add(TagAlias(source=ids["src"], target=ids["tgt2"]))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

    def test_suggest_aliases_finds_similar_tags(self, session_factory):
        with session_factory() as s:
            for name, rc in [
                ("Fate/Grand Order", 100), ("Fate Grand Order", 50),
                ("NTR", 80), ("ntr", 40),
                ("completely-unrelated", 10), ("completely-different", 5),
            ]:
                s.add(Tag(name=name, reference_count=rc))
            s.commit()
            repo = SQLAlchemyTagRepository(s)
            result = asyncio.run(repo.suggest_aliases(limit=10, offset=0))
            assert "items" in result and "next_offset" in result
            targets = {item["target"]["name"] for item in result["items"]}
            assert "Fate/Grand Order" in targets or "Fate Grand Order" in targets
            assert "NTR" in targets or "ntr" in targets

    def test_suggest_aliases_treats_wildcards_as_literals(self, session_factory):
        """``_`` / ``%`` inside a tag name are literals, never LIKE wildcards.

        The old probe built ``'%BanG_Dream!%'`` verbatim, so the ``_`` matched
        any single character (``xbangGdream!`` came back as "similar" although
        it does not contain the name) and ``%高中生`` — a tag that really
        exists — matched all 75,186 other tags, surfacing the globally hottest
        ones as its candidates.
        """
        with session_factory() as s:
            for name, rc in [
                ("BanG_Dream!", 100),
                ("bang_dream!", 90),
                ("zzBanG_Dream!zz", 80),
                ("xbangGdream!", 70),   # only the `_` wildcard could match this
                ("Xbanggdream!", 60),   # …or ignoring it entirely
                ("%高中生", 50),
                ("xx%高中生yy", 40),     # the only tag containing it literally
                ("催眠", 1000),         # globally hottest — must stay out
            ]:
                s.add(Tag(name=name, reference_count=rc))
            s.commit()
            repo = SQLAlchemyTagRepository(s)

            item = asyncio.run(
                repo.suggest_aliases(target_tag="BanG_Dream!")
            )["items"][0]
            names = {c["name"] for c in item["candidates"]}
            assert "bang_dream!" in names          # literal substring
            assert "zzBanG_Dream!zz" in names      # literal substring, other 1st char
            assert "xbangGdream!" not in names
            assert "Xbanggdream!" not in names

            item = asyncio.run(
                repo.suggest_aliases(target_tag="%高中生")
            )["items"][0]
            names = {c["name"] for c in item["candidates"]}
            assert names == {"xx%高中生yy"}
            assert "催眠" not in names

    def test_suggest_aliases_cursor_resumes_without_overlap_or_gaps(
        self, session_factory,
    ):
        """Consecutive pages over **tied** ``reference_count`` values.

        Production shape: 39,400 tags sit at ``rc=1``, so ordering by
        ``reference_count`` alone is not a total order — which is why
        consecutive ``OFFSET`` pages used to overlap (measured offset
        450∩500={901}) and leak (13,748 tags never examined).  ``next_offset``
        is now a cursor over ``(reference_count DESC, id DESC)``.
        """
        with session_factory() as s:
            for i in range(40):
                s.add(Tag(name=f"tie-{i:02d}", reference_count=1))
            s.commit()
            repo = SQLAlchemyTagRepository(s)

            seen: list[int] = []
            cursor = 0
            for _ in range(20):  # 40 tags / limit 5 → 8 pages (+ the empty tail page)
                page = asyncio.run(repo.suggest_aliases(limit=5, offset=cursor))
                seen.extend(item["target"]["id"] for item in page["items"])
                cursor = page["next_offset"]
                if cursor == 0:
                    break

            expected = list(s.execute(
                select(Tag.id).where(Tag.name.like("tie-%")).order_by(Tag.id.desc())
            ).scalars())
            assert cursor == 0, "cursor must reach the end of the table"
            assert seen == expected, "pages overlap or skip targets"
            assert len(seen) == len(set(seen)) == 40

    def test_suggest_aliases_excludes_aliased_tags_before_candidate_limit(
        self, session_factory,
    ):
        """Alias-excluded tags are dropped in SQL, i.e. *before* ``LIMIT 50``.

        The old probe took the 50 hottest matches and only then filtered the
        excluded ones in Python, so a target whose hottest 50 matches were all
        aliased came back with zero candidates even though more matches
        existed (the extra ones are reachable only past the limit).
        """
        with session_factory() as s:
            s.add(Tag(name="q-target", reference_count=5))
            s.add(Tag(name="q-dummy", reference_count=0))
            for i in range(60):
                s.add(Tag(name=f"q-filler-{i:02d}", reference_count=1000 - i))
            s.commit()
            target_id = s.execute(
                select(Tag.id).where(Tag.name == "q-target")
            ).scalar_one()
            dummy_id = s.execute(
                select(Tag.id).where(Tag.name == "q-dummy")
            ).scalar_one()
            fillers = list(s.execute(
                select(Tag).where(Tag.name.like("q-filler-%"))
            ).scalars())
            # Alias away the 50 hottest matches (rc 1000 … 951).
            for filler in sorted(fillers, key=lambda t: -t.reference_count)[:50]:
                s.add(TagAlias(source=filler.id, target=dummy_id))
            s.commit()

            result = asyncio.run(
                SQLAlchemyTagRepository(s).suggest_aliases(target_tag="q-target")
            )
            assert len(result["items"]) == 1
            assert [c["name"] for c in result["items"][0]["candidates"]] == [
                f"q-filler-{i:02d}" for i in range(50, 60)
            ]
            assert target_id not in {
                c["id"] for c in result["items"][0]["candidates"]
            }

    def test_suggest_aliases_target_mode_ignores_offset(self, session_factory):
        """``target_tag=`` names one target: no page to resume, ``offset`` moot."""
        with session_factory() as s:
            for i in range(6):
                s.add(Tag(name=f"m-tag-{i}", reference_count=10 - i))
            s.commit()
            repo = SQLAlchemyTagRepository(s)

            first = asyncio.run(repo.suggest_aliases(target_tag="m-tag-0", offset=0))
            late = asyncio.run(
                repo.suggest_aliases(target_tag="m-tag-0", offset=99999)
            )
            assert first == late
            assert first["next_offset"] == 0
            assert [i["target"]["name"] for i in first["items"]] == ["m-tag-0"]

            # A tag that already participates in an alias mapping has no
            # suggestion — unchanged early-return behaviour.
            ids = {t.name: t.id for t in s.execute(select(Tag)).scalars()}
            s.add(TagAlias(source=ids["m-tag-1"], target=ids["m-tag-0"]))
            s.commit()
            for name in ("m-tag-0", "m-tag-1"):
                assert asyncio.run(
                    repo.suggest_aliases(target_tag=name)
                ) == {"items": [], "next_offset": 0}
            assert asyncio.run(
                repo.suggest_aliases(target_tag="does-not-exist")
            ) == {"items": [], "next_offset": 0}


class TestTagSuggestPatterns:
    """``_like_patterns`` + the SQL shape the bigram index needs (no DB)."""

    def test_patterns_are_escaped(self):
        assert _like_patterns("%高中生") == ("\\%%", "%\\%高中生%")
        assert _like_patterns("BanG_Dream!") == ("B%", "%BanG\\_Dream!%")
        assert _like_patterns("R-18") == ("R%", "%R-18%")
        assert _like_patterns("") == ("", "")

    def test_candidate_sql_uses_search_lower_and_escape(self):
        r"""The predicate must stay on the generated column, with ``ESCAPE``.

        ``tag.search_lower`` is what migration 0008's ``tag_search_bigm`` GIN
        index covers, and the escaper's ``\`` only means anything next to
        ``ESCAPE '\\'`` — switching back to ``tag.name ILIKE`` silently
        reintroduces the full table scan and the wildcard bug.
        """
        for stmt in (tag_repo._EXPAND_SQL, tag_repo._TARGET_SQL):
            sql = str(stmt)
            assert "search_lower LIKE lower(" in sql
            assert "ESCAPE '\\'" in sql
            assert "name ILIKE" not in sql


@pytest.mark.slow
class TestConcurrentAccess:
    def test_concurrent_reads(self, session_factory):
        """10 threads reading simultaneously should not error."""
        import concurrent.futures

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="concurrent_test"))
            s.flush()
            for i in range(100):
                s.add(Novel(id=i + 1, title=f"N{i}", author_id=1,
                            path=f"/tmp/c{i}.txt"))
            s.commit()

        errors = []

        def read_page():
            try:
                with session_factory() as s:
                    repo = SQLAlchemyNovelRepository(s)
                    from copixiv.core.services import QuerySpec
                    result = asyncio.run(repo.get_novels(
                        QuerySpec(order_by="id", per_page=10),
                    ))
                    return len(result.get("novels", []))
            except Exception as e:
                errors.append(str(e))
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(read_page) for _ in range(10)]
            results = [f.result() for f in futures]

        assert not errors, f"Concurrent reads failed: {errors}"
        assert all(r > 0 for r in results), f"Expected non-empty pages, got {results}"


class TestSQLAlchemyTaskRepository:
    def test_add_and_update_task_sync(self, session_factory):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        with session_factory() as s:
            repo = SQLAlchemyTaskRepository(s)
            task_id = repo.add_task_sync("test-task", {"key": "value"}, "test-task")
            s.commit()
            assert task_id > 0
            repo.update_task_sync(task_id, "running")
            s.commit()
            task = s.get(TaskHistory, task_id)
            assert task is not None
            assert task.name == "test-task"
            assert task.status == "running"

    def test_enqueue_same_task_func_twice_inserts_two_pending_rows(
        self, session_factory,
    ):
        """A2 regression: the task-dedup partial unique index is gone.

        Enqueuing the same ``task_func`` again must insert a second pending
        row (no ``IntegrityError``) — duplicates now queue up.
        """
        from copixiv.tasks.kernel import TaskHistoryRecorder

        recorder = TaskHistoryRecorder(session_factory)
        first_id = recorder.enqueue("dup-task", {}, "dup_task_func")
        second_id = recorder.enqueue("dup-task", {}, "dup_task_func")

        assert first_id != second_id
        with session_factory() as s:
            rows = s.execute(
                select(TaskHistory)
                .where(TaskHistory.task_func == "dup_task_func")
                .order_by(TaskHistory.id)
            ).scalars().all()
        assert [r.id for r in rows] == [first_id, second_id]
        assert [r.status for r in rows] == ["pending", "pending"]

    def test_update_task_with_result_and_duration(self, session_factory):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        with session_factory() as s:
            repo = SQLAlchemyTaskRepository(s)
            task_id = repo.add_task_sync("dur-test", {"a": 1}, "dur-test")
            s.commit()
            result = json.dumps({"log": "ok", "new_novels_count": 5})
            repo.update_task_sync(task_id, "success", result=result, duration=12.5)
            s.commit()
            task = s.get(TaskHistory, task_id)
            assert task.status == "success"
            assert task.duration == 12.5
            assert task.result["new_novels_count"] == 5

    def test_get_scheduled_tasks_sync(self, session_factory):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        with session_factory() as s:
            s.add(ScheduledTask(name="daily", task="novel_follow",
                                cron="0 3 * * *", is_enabled=True, sort_index=0))
            s.add(ScheduledTask(name="weekly", task="novel_search",
                                cron="20 4 * * 1", is_enabled=False, sort_index=1))
            s.commit()
            repo = SQLAlchemyTaskRepository(s)
            tasks = repo.get_scheduled_tasks_sync()
            assert len(tasks) == 2
            assert tasks[0].name == "daily"
            assert tasks[1].name == "weekly"

    def test_crud_scheduled_task(self, session_factory):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        with session_factory() as s:
            repo = SQLAlchemyTaskRepository(s)
            created = asyncio.run(repo.create_scheduled({
                "name": "crud-test", "task": "novel_follow",
                "cron": "0 6 * * *", "is_enabled": True, "sort_index": 2,
            }))
            s.commit()
            assert created.id > 0
            updated = asyncio.run(repo.update_scheduled(created.id, {
                "is_enabled": False, "sort_index": 99,
            }))
            s.commit()
            t = asyncio.run(repo.get_scheduled_tasks())
            found = next(x for x in t if x.id == created.id)
            assert found.is_enabled is False
            assert found.sort_index == 99
            deleted = asyncio.run(repo.delete_scheduled(created.id))
            s.commit()
            assert deleted is True
            assert asyncio.run(repo.delete_scheduled(9999)) is False

    def test_reorder_scheduled(self, session_factory):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        with session_factory() as s:
            t1 = ScheduledTask(name="a", task="x", cron="* * * * *", sort_index=0)
            t2 = ScheduledTask(name="b", task="y", cron="* * * * *", sort_index=1)
            s.add_all([t1, t2])
            s.commit()
            repo = SQLAlchemyTaskRepository(s)
            matched = asyncio.run(repo.reorder_scheduled([t2.id, t1.id]))
            assert matched == 2
            s.commit()
            tasks = repo.get_scheduled_tasks_sync()
            assert tasks[0].name == "b"
            assert tasks[1].name == "a"


class TestTaskManagerHelpers:
    def test_normalize_result_none(self):
        from copixiv.tasks.kernel import TaskExecutor
        r = TaskExecutor._normalize_result(None)
        assert r.summary == "完成"

    def test_construction_strips_none_deps(self):
        from copixiv.tasks.kernel import TaskManagerSystem
        tms = TaskManagerSystem(
            session_factory=lambda: None,
            client="cli", file_storage=None, image_downloader=None,
            epub_builder=None, config=None,
        )
        deps = tms._executor._deps
        assert deps["client"] == "cli"
        assert deps["file_storage"] is None
        assert "write_lock" in deps


class TestFailedNovelRepository:
    """FailedNovel ledger: record enrichment, list ordering, count, clear."""

    def _seed_novel_with_failed(self, session_factory, novel_id, **failed_kwargs):
        with session_factory() as s:
            s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
            s.flush()
            s.add(Novel(id=novel_id, title=f"标题{novel_id}",
                        author_id=novel_id, path=f"/tmp/{novel_id}.txt"))
            s.flush()
            s.add(FailedNovel(novel_id=novel_id, failure_type="download",
                              error_message="e", failed_times=1, **failed_kwargs))
            s.commit()

    def test_record_sets_title_and_timestamp(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1001, author_name="作者"))
            s.flush()
            s.add(Novel(id=1001, title="标题A", author_id=1001,
                        path="/tmp/1001.txt"))
            s.flush()
            repo = FailedNovelRepository(s)
            repo.record(1001, "download", "boom", title="标题A")
            s.commit()
            row = s.get(FailedNovel, 1001)
            assert row.title == "标题A"
            assert row.last_failed_at is not None
            assert row.failed_times == 1

    def test_record_increments_and_keeps_old_title(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1002, author_name="作者"))
            s.flush()
            s.add(Novel(id=1002, title="标题B", author_id=1002,
                        path="/tmp/1002.txt"))
            s.flush()
            repo = FailedNovelRepository(s)
            repo.record(1002, "download", "boom1", title="标题B")
            s.commit()
            repo.record(1002, "download", "boom2")
            s.commit()
            row = s.get(FailedNovel, 1002)
            assert row.failed_times == 2
            assert row.title == "标题B"
            assert row.error_message == "boom2"

    def test_record_allows_non_persisted_novel(self, session_factory):
        """A never-persisted novel CAN be ledgered (no FK by design) — a
        download failure must never be silently dropped."""
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            repo = FailedNovelRepository(s)
            repo.record(999999, "download", "boom")
            s.commit()
            row = s.get(FailedNovel, 999999)
            assert row is not None
            assert row.failed_times == 1
            s.delete(row)
            s.commit()

    def test_list_orders_by_last_failed_at_desc_nulls_last(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        self._seed_novel_with_failed(session_factory, 1, last_failed_at=None)
        self._seed_novel_with_failed(
            session_factory, 2,
            last_failed_at=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
        )
        with session_factory() as s:
            repo = FailedNovelRepository(s)
            items = repo.list()
            assert [i.novel_id for i in items] == [2, 1]

    def test_list_sorts_not_found_family_last(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            for nid, msg, title in [
                (1, "EPUB 生成失败: novel 1", "可修复"),
                (2, "Page not found", "删除A"),
                (3, "webview_novel 返回空", "删除B"),
            ]:
                s.add(Author(author_id=nid, author_name=f"作者{nid}"))
                s.flush()
                s.add(Novel(id=nid, title=f"标题{nid}", author_id=nid,
                            path=f"/tmp/{nid}.txt"))
                s.flush()
                s.add(FailedNovel(
                    novel_id=nid, failure_type="download", error_message=msg,
                    failed_times=1, title=title,
                    last_failed_at=datetime(2026, 8, 19, 20, 55 + nid,
                                            tzinfo=timezone.utc),
                ))
            s.commit()
            repo = FailedNovelRepository(s)
            items = repo.list()
            # Actionable (EPUB) first; not-found family last, newest time first.
            assert [i.novel_id for i in items] == [1, 3, 2]

    def test_count_and_clear_all(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            for nid in (1, 2):
                s.add(Author(author_id=nid, author_name=f"作者{nid}"))
                s.flush()
                s.add(Novel(id=nid, title=f"标题{nid}", author_id=nid,
                            path=f"/tmp/{nid}.txt"))
            s.flush()
            repo = FailedNovelRepository(s)
            repo.record(1, "download", "e1")
            repo.record(2, "download", "e2")
            s.commit()
            assert repo.count() == 2
            assert repo.clear_all() == 2
            s.commit()
            assert repo.count() == 0

    def test_reset_count_keeps_record(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="作者"))
            s.flush()
            s.add(Novel(id=1, title="标题", author_id=1, path="/tmp/1.txt"))
            s.flush()
            repo = FailedNovelRepository(s)
            repo.record(1, "download", "e1", title="标题")
            repo.record(1, "download", "e2")
            repo.record(1, "download", "e3")
            s.commit()
            row = s.get(FailedNovel, 1)
            assert row.failed_times == 3
            repo.reset_count(1)
            s.commit()
            row = s.get(FailedNovel, 1)
            assert row is not None
            assert row.failed_times == 0
            assert row.title == "标题"

    def test_forget_removes_single_record(self, session_factory):
        from copixiv.features.failures.repo import FailedNovelRepository

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="作者"))
            s.flush()
            s.add(Novel(id=1, title="标题", author_id=1, path="/tmp/1.txt"))
            s.flush()
            repo = FailedNovelRepository(s)
            repo.record(1, "download", "e1")
            s.commit()
            repo.forget(1)
            s.commit()
            assert s.get(FailedNovel, 1) is None
