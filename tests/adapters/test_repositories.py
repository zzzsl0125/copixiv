"""Integration tests for repositories (in-memory SQLite)."""

import asyncio
import pytest
from sqlalchemy.exc import IntegrityError

from copixiv.db.models import (
    Novel, Author, Tag, TagAlias, Favourite,
    TaskHistory, ScheduledTask, FailedNovel,
)
from copixiv.db.engine import create_session_factory
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository

# engine (sqlite_engine) and file_engine come from tests/conftest.py.


@pytest.fixture
def session(sqlite_engine):
    sf = create_session_factory(sqlite_engine)
    s = sf()
    yield s
    s.close()


class TestSQLAlchemyNovelRepository:
    def test_upsert_new_novel(self, session):
        session.add(Author(author_id=10, author_name="Test Author"))
        session.commit()

        repo = SQLAlchemyNovelRepository(session)
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

    def test_upsert_novel_model_with_serializer_nulled(self, session):
        """Regression (2026-08-19 cron failures): upsert must not depend on
        ``Novel.__pydantic_serializer__``.

        pydantic v2's ``model_rebuild`` *deletes and recreates*
        ``__pydantic_serializer__`` and is explicitly not thread-safe.  In a
        worker thread (``asyncio.to_thread``) it can transiently leave the
        serializer as ``None``, and ``model_dump()`` then raises
        ``TypeError: 'None' is not an instance of 'SchemaSerializer'`` — the
        exact failure that took down the 每日更新 / 每日排行 cron runs.
        ``upsert_novels`` reads field values from ``__dict__``
        (serializer-free), so a ``None`` serializer must NOT break the upsert
        (where the old ``model_dump()`` path would have raised).
        """
        from pydantic_core import SchemaSerializer
        from copixiv.core.models import Novel as NovelModel, EpubStatus

        session.add(Author(author_id=10, author_name="Test Author"))
        session.commit()

        novel = NovelModel(
            id=1, title="带图的小说", author_id=10, path="/tmp/n1.txt",
            has_epub=EpubStatus.PENDING, tags=["R-18", "NTR"],
            content="正文" * 100,
        )

        # Simulate the pydantic thread-rebuild race: serializer transiently
        # None.  Construction already happened (uses the validator, not the
        # serializer), so nulling the serializer afterward is safe.
        try:
            NovelModel.__pydantic_serializer__ = None
            repo = SQLAlchemyNovelRepository(session)
            count = asyncio.run(repo.upsert_novels([novel]))
            session.commit()
        finally:
            # Restore the real serializer.  force=True is required because
            # __pydantic_complete__ is still True (we only nulled the
            # serializer), so a non-forced rebuild would no-op.
            NovelModel.model_rebuild(force=True)

        # Sanity: the class is whole again for the rest of the session.
        assert NovelModel.__pydantic_complete__ is True
        assert isinstance(NovelModel.__pydantic_serializer__, SchemaSerializer)

        assert count == 1
        row = session.get(Novel, 1)
        assert row is not None
        assert row.title == "带图的小说"
        assert int(row.has_epub) == int(EpubStatus.PENDING)  # PENDING == 1

    def test_get_existing_ids(self, session):
        session.add(Author(author_id=1, author_name="Test Author"))
        session.add(Novel(id=1, title="A", author_id=1, path="/tmp/a.txt"))
        session.add(Novel(id=2, title="B", author_id=1, path="/tmp/b.txt"))
        session.commit()

        repo = SQLAlchemyNovelRepository(session)
        existing = asyncio.run(repo.get_existing_ids({1, 2, 3}))
        assert existing == {1, 2}

    def test_toggle_favourite(self, session):
        session.add(Author(author_id=1, author_name="Test Author"))
        session.add(Novel(id=1, title="F", author_id=1, path="/tmp/f.txt"))
        session.commit()

        repo = SQLAlchemyNovelRepository(session)

        asyncio.run(repo.toggle_favourite(1))
        session.commit()

        fav = session.get(Favourite, 1)
        assert fav is not None

        asyncio.run(repo.toggle_favourite(1))
        session.commit()
        assert session.get(Favourite, 1) is None


class TestSQLAlchemyAuthorRepository:
    def test_get_by_id(self, session):
        session.add(Author(author_id=99, author_name="Test Author"))
        session.commit()

        repo = SQLAlchemyAuthorRepository(session)
        author = asyncio.run(repo.get_by_id(99))
        assert author is not None
        assert author["author_name"] == "Test Author"

    def test_need_update_new_author(self, session):
        repo = SQLAlchemyAuthorRepository(session)
        assert asyncio.run(repo.need_update(999)) is True

    def test_need_update_with_date(self, session):
        from datetime import date
        session.add(Author(
            author_id=5, author_name="Old",
            last_update=date.today().isoformat(),
        ))
        session.commit()

        repo = SQLAlchemyAuthorRepository(session)
        assert asyncio.run(repo.need_update(5)) is False


class TestSQLAlchemyTagRepository:
    """Phase 3: Tests for tag aliases with integer FK normalization."""

    def _ensure_tags(self, session, *names: str) -> dict[str, int]:
        """Ensure tags exist and return {name: id} mapping."""
        repo = SQLAlchemyTagRepository(session)
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

        repo = SQLAlchemyTagRepository(session)
        alias_map = repo.get_alias_map_sync()
        assert alias_map.get("R-18") == "R18"
        assert alias_map.get("NTR") == "ntr"

    def test_create_alias_via_repository(self, session):
        """create_alias should accept tag names and store as integer FKs."""
        ids = self._ensure_tags(session, "source_tag", "target_tag")

        repo = SQLAlchemyTagRepository(session)
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
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_unique_source_constraint(self, session):
        """Two aliases with the same source should fail."""
        ids = self._ensure_tags(session, "src", "tgt1", "tgt2")

        session.add(TagAlias(source=ids["src"], target=ids["tgt1"]))
        session.commit()

        session.add(TagAlias(source=ids["src"], target=ids["tgt2"]))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_suggest_aliases_finds_similar_tags(self, session):
        """suggest_aliases should find pairs of tags with similar names."""
        repo = SQLAlchemyTagRepository(session)

        # Create tags with varying reference counts and similarity
        from copixiv.db.models import Tag as TagModel
        tag_data = [
            ("Fate/Grand Order", 100),
            ("Fate Grand Order", 50),
            ("NTR", 80),
            ("ntr", 40),
            ("completely-unrelated", 10),
            ("completely-different", 5),
        ]
        for name, rc in tag_data:
            session.add(TagModel(name=name, reference_count=rc))
        session.commit()

        result = asyncio.run(repo.suggest_aliases(limit=10, offset=0))

        assert "items" in result
        assert "next_offset" in result

        # "Fate/Grand Order" should pair with "Fate Grand Order"
        # "NTR" should pair with "ntr"
        items = result["items"]
        assert len(items) >= 2, f"Expected at least 2 suggestions, got {len(items)}"

        targets = {item["target"]["name"] for item in items}
        assert "Fate/Grand Order" in targets or "Fate Grand Order" in targets
        assert "NTR" in targets or "ntr" in targets

    def test_suggest_aliases_excludes_aliased_tags(self, session):
        """Tags already in an alias mapping should be excluded from suggestions."""
        repo = SQLAlchemyTagRepository(session)

        from copixiv.db.models import Tag as TagModel
        tag_data = [
            ("already-mapped-source", 50),
            ("already-mapped-target", 50),
            ("already-mapped-free", 40),
            ("Already Free Tag", 30),
        ]
        for name, rc in tag_data:
            session.add(TagModel(name=name, reference_count=rc))
        session.commit()

        ids = self._ensure_tags(session, "already-mapped-source",
                                "already-mapped-target")
        session.add(TagAlias(source=ids["already-mapped-source"],
                             target=ids["already-mapped-target"]))
        session.commit()

        result = asyncio.run(repo.suggest_aliases(limit=10, offset=0))

        # Neither already-mapped-source nor already-mapped-target should appear
        all_names: set[str] = set()
        for item in result["items"]:
            all_names.add(item["target"]["name"])
            for c in item["candidates"]:
                all_names.add(c["name"])
        assert "already-mapped-source" not in all_names
        assert "already-mapped-target" not in all_names
        # Free tags starting with "a" should appear
        assert "already-mapped-free" in all_names or "Already Free Tag" in all_names

    def test_suggest_aliases_with_target_tag_filter(self, session):
        """When target_tag is specified, only suggestions for that tag return."""
        repo = SQLAlchemyTagRepository(session)

        from copixiv.db.models import Tag as TagModel
        tag_data = [
            ("NTR", 100),
            ("ntr", 50),
            ("R-18", 80),
            ("R18", 40),
        ]
        for name, rc in tag_data:
            session.add(TagModel(name=name, reference_count=rc))
        session.commit()

        result = asyncio.run(
            repo.suggest_aliases(limit=10, offset=0, target_tag="NTR")
        )

        items = result["items"]
        assert len(items) == 1
        assert items[0]["target"]["name"] == "NTR"
        cand_names = [c["name"] for c in items[0]["candidates"]]
        assert "ntr" in cand_names


@pytest.mark.slow
class TestConcurrentAccess:
    """Phase 4: Concurrent access should not cause 'database is locked'."""

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

        def read_page():
            try:
                with Session() as s:
                    repo = SQLAlchemyNovelRepository(s)
                    from copixiv.core.services import QuerySpec
                    result = asyncio.run(repo.get_novels(
                        QuerySpec(order_by="id", per_page=10)
                    ))
                    return len(result.get("novels", []))
            except Exception as e:
                errors.append(str(e))
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(read_page) for _ in range(10)]
            results = [f.result() for f in futures]

        assert not errors, f"Concurrent reads failed: {errors}"
        assert all(r == 10 for r in results[:9]), \
            f"Expected 10 novels per page, got {results}"


class TestSQLAlchemyTaskRepository:
    def test_add_and_update_task_sync(self, session):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)

        # Add
        task_id = repo.add_task_sync("test-task", {"key": "value"}, "test-task")
        session.commit()
        assert task_id > 0

        # Update status
        repo.update_task_sync(task_id, "running")
        session.commit()

        # Verify
        task = session.get(TaskHistory, task_id)
        assert task is not None
        assert task.name == "test-task"
        assert task.status == "running"

    def test_update_task_with_result_and_duration(self, session):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
        import json

        repo = SQLAlchemyTaskRepository(session)
        task_id = repo.add_task_sync("dur-test", {"a": 1}, "dur-test")
        session.commit()

        result = json.dumps({"log": "ok", "new_novels_count": 5})
        repo.update_task_sync(task_id, "success", result=result, duration=12.5)
        session.commit()

        task = session.get(TaskHistory, task_id)
        assert task.status == "success"
        assert task.duration == 12.5
        assert "new_novels_count" in task.result

    def test_get_scheduled_tasks_sync(self, session):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)
        # Insert a couple of scheduled tasks
        models = __import__(
            "copixiv.db.models", fromlist=["ScheduledTask"]
        )
        session.add(ScheduledTask(
            name="daily", task="novel_follow", cron="0 3 * * *",
            is_enabled=True, sort_index=0,
        ))
        session.add(ScheduledTask(
            name="weekly", task="novel_search", cron="20 4 * * 1",
            is_enabled=False, sort_index=1,
        ))
        session.commit()

        tasks = repo.get_scheduled_tasks_sync()
        assert len(tasks) == 2
        assert tasks[0].name == "daily"
        assert tasks[1].name == "weekly"

    def test_crud_scheduled_task(self, session):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)

        # Create
        created = asyncio.run(repo.create_scheduled({
            "name": "crud-test", "task": "novel_follow",
            "cron": "0 6 * * *", "is_enabled": True, "sort_index": 2,
        }))
        session.commit()
        assert created.id > 0

        # Update
        updated = asyncio.run(repo.update_scheduled(created.id, {
            "is_enabled": False, "sort_index": 99,
        }))
        session.commit()
        fetched = asyncio.run(repo.get_scheduled_tasks())
        t = next(t for t in fetched if t.id == created.id)
        assert t.is_enabled == False
        assert t.sort_index == 99

        # Delete
        deleted = asyncio.run(repo.delete_scheduled(created.id))
        session.commit()
        assert deleted is True
        assert asyncio.run(repo.delete_scheduled(9999)) is False

    def test_reorder_scheduled(self, session):
        from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)
        t1 = ScheduledTask(name="a", task="x", cron="* * * * *", sort_index=0)
        t2 = ScheduledTask(name="b", task="y", cron="* * * * *", sort_index=1)
        session.add_all([t1, t2])
        session.commit()

        matched = asyncio.run(repo.reorder_scheduled([t2.id, t1.id]))
        assert matched == 2
        session.commit()

        tasks = repo.get_scheduled_tasks_sync()
        assert tasks[0].sort_index == 0
        assert tasks[1].sort_index == 1
        # Order should be reversed from original
        assert tasks[0].name == "b"
        assert tasks[1].name == "a"


class TestTaskManagerHelpers:
    """Unit tests for task-kernel helper methods (no scheduler needed).

    ``parse_params`` now lives on TaskHistoryRecorder (kernel.py),
    ``_normalize_result`` on TaskExecutor (kernel.py), and the
    dependency dict on the executor (docs/MODULARITY.md §M8).
    """

    def test_parse_json_string(self):
        from copixiv.tasks.kernel import TaskHistoryRecorder
        assert TaskHistoryRecorder.parse_params('{"a": 1}') == {"a": 1}
        assert TaskHistoryRecorder.parse_params("not json") == {}

    def test_parse_json_dict_passthrough(self):
        from copixiv.tasks.kernel import TaskHistoryRecorder
        assert TaskHistoryRecorder.parse_params({"a": 1}) == {"a": 1}
        assert TaskHistoryRecorder.parse_params(None) == {}

    def test_normalize_result_taskresult_passthrough(self):
        from copixiv.core.models import TaskResult
        from copixiv.tasks.kernel import TaskExecutor
        tr = TaskResult(summary="done", new_novel_titles=["a"])
        assert TaskExecutor._normalize_result(tr) is tr

    def test_normalize_result_none(self):
        from copixiv.tasks.kernel import TaskExecutor
        r = TaskExecutor._normalize_result(None)
        assert r.summary == "完成"

    def test_construction_strips_none_deps(self):
        from copixiv.tasks.kernel import TaskManagerSystem

        tms = TaskManagerSystem(
            session_factory=lambda: None,
            client="cli",
            file_storage=None,
            image_downloader=None,
            epub_builder=None,
            config=None,
        )
        deps = tms._executor._deps
        assert deps["client"] == "cli"
        assert deps["file_storage"] is None
        assert "write_lock" in deps

    def test_construction_keeps_all_deps(self):
        from copixiv.tasks.kernel import TaskManagerSystem

        tms = TaskManagerSystem(
            session_factory=lambda: None,
            client="a", file_storage="b", image_downloader="c",
            epub_builder="d", config="e",
        )
        deps = tms._executor._deps
        assert {
            k: deps[k]
            for k in ("client", "file_storage", "image_downloader",
                      "epub_builder", "config")
        } == {
            "client": "a", "file_storage": "b",
            "image_downloader": "c", "epub_builder": "d", "config": "e",
        }
        # write_lock is always added by the manager itself
        assert "write_lock" in deps



class TestFailedNovelRepository:
    """FailedNovel ledger: record enrichment, list ordering, count, clear."""

    @pytest.fixture
    def repo(self, session):
        from copixiv.features.failures.repo import FailedNovelRepository
        return FailedNovelRepository(session)

    def test_record_sets_title_and_timestamp(self, session, repo):
        repo.record(1001, "download", "boom", title="标题A")
        session.commit()
        row = session.get(FailedNovel, 1001)
        assert row.title == "标题A"
        assert row.last_failed_at is not None
        assert row.failed_times == 1

    def test_record_increments_and_keeps_old_title(self, session, repo):
        repo.record(1002, "download", "boom1", title="标题B")
        session.commit()
        repo.record(1002, "download", "boom2")  # 无 title → 保留旧 title
        session.commit()
        row = session.get(FailedNovel, 1002)
        assert row.failed_times == 2
        assert row.title == "标题B"
        assert row.error_message == "boom2"

    def test_list_orders_by_last_failed_at_desc_nulls_last(self, session, repo):
        repo.record(1, "download", "e1", title="旧记录")
        # 手动把 last_failed_at 置空模拟迁移前存量
        row = session.get(FailedNovel, 1)
        row.last_failed_at = None
        session.commit()
        repo.record(2, "download", "e2", title="新记录")
        session.commit()
        items = repo.list()
        assert [i.novel_id for i in items] == [2, 1]

    def test_list_sorts_not_found_family_last(self, session, repo):
        """Page-not-found 家族（删除/不可获取）排在可处理失败之后。"""
        repo.record(1, "download", "EPUB 生成失败: novel 1", title="可修复")
        repo.record(2, "download", "Page not found", title="删除A")
        repo.record(3, "download", "webview_novel 返回空", title="删除B")
        session.commit()
        # 手动固定时间：可修复的在 20:55，删除家族更新更晚
        row = session.get(FailedNovel, 1)
        row.last_failed_at = "2026-08-19 20:55:00"
        row2 = session.get(FailedNovel, 2)
        row2.last_failed_at = "2026-08-19 20:57:00"
        row3 = session.get(FailedNovel, 3)
        row3.last_failed_at = "2026-08-19 20:56:00"
        session.commit()

        items = repo.list()
        # 可处理的在前；not-found 家族在后，且其内部按时间倒序
        assert [i.novel_id for i in items] == [1, 2, 3]

    def test_count_and_clear_all(self, session, repo):
        repo.record(1, "download", "e1")
        repo.record(2, "download", "e2")
        session.commit()
        assert repo.count() == 2
        assert repo.clear_all() == 2
        session.commit()
        assert repo.count() == 0

    def test_reset_count_keeps_record(self, session, repo):
        repo.record(1, "download", "e1", title="标题")
        repo.record(1, "download", "e2")
        repo.record(1, "download", "e3")
        session.commit()
        row = session.get(FailedNovel, 1)
        assert row.failed_times == 3

        repo.reset_count(1)
        session.commit()

        # 记录保留（标题、错误、时间），只有计数归零
        row = session.get(FailedNovel, 1)
        assert row is not None
        assert row.failed_times == 0
        assert row.title == "标题"
        assert row.error_message == "e3"

    def test_reset_all_keeps_records(self, session, repo):
        repo.record(1, "download", "e1")
        repo.record(2, "download", "e2")
        session.commit()
        assert repo.reset_all() == 2
        session.commit()
        assert repo.count() == 2
        assert repo.list()[0].failed_times == 0
        assert repo.list()[1].failed_times == 0

    def test_forget_removes_single_record(self, session, repo):
        repo.record(1, "download", "e1")
        session.commit()
        repo.forget(1)
        session.commit()
        assert session.get(FailedNovel, 1) is None
