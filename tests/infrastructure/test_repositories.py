"""Integration tests for repositories (in-memory SQLite)."""

import asyncio
import pytest
from sqlalchemy.exc import IntegrityError

from copixiv.infrastructure.database.models import (
    Novel, Author, Tag, TagAlias, Favourite,
    TaskHistory, ScheduledTask,
)
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository
from copixiv.infrastructure.repositories.author import SQLAlchemyAuthorRepository
from copixiv.infrastructure.repositories.tag import SQLAlchemyTagRepository

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
        from copixiv.infrastructure.database.models import Tag as TagModel
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

        from copixiv.infrastructure.database.models import Tag as TagModel
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

        from copixiv.infrastructure.database.models import Tag as TagModel
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
                    from copixiv.domain.services.query_spec import QuerySpec
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
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)

        # Add
        task_id = repo.add_task_sync("test-task", {"key": "value"})
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
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository
        import json

        repo = SQLAlchemyTaskRepository(session)
        task_id = repo.add_task_sync("dur-test", {"a": 1})
        session.commit()

        result = json.dumps({"log": "ok", "new_novels_count": 5})
        repo.update_task_sync(task_id, "success", result=result, duration=12.5)
        session.commit()

        task = session.get(TaskHistory, task_id)
        assert task.status == "success"
        assert task.duration == 12.5
        assert "new_novels_count" in task.result

    def test_get_scheduled_tasks_sync(self, session):
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

        repo = SQLAlchemyTaskRepository(session)
        # Insert a couple of scheduled tasks
        models = __import__(
            "copixiv.infrastructure.database.models", fromlist=["ScheduledTask"]
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
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

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
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

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

    ``parse_params`` now lives on TaskHistoryRecorder (kernel/history.py),
    ``_normalize_result`` on TaskExecutor (kernel/executor.py), and the
    dependency dict on the executor (docs/MODULARITY.md §M8).
    """

    def test_parse_json_string(self):
        from copixiv.tasks.history import TaskHistoryRecorder
        assert TaskHistoryRecorder.parse_params('{"a": 1}') == {"a": 1}
        assert TaskHistoryRecorder.parse_params("not json") == {}

    def test_parse_json_dict_passthrough(self):
        from copixiv.tasks.history import TaskHistoryRecorder
        assert TaskHistoryRecorder.parse_params({"a": 1}) == {"a": 1}
        assert TaskHistoryRecorder.parse_params(None) == {}

    def test_normalize_result_taskresult_passthrough(self):
        from copixiv.domain.models.task_result import TaskResult
        from copixiv.tasks.executor import TaskExecutor
        tr = TaskResult(summary="done", new_novel_titles=["a"])
        assert TaskExecutor._normalize_result(tr) is tr

    def test_normalize_result_none(self):
        from copixiv.tasks.executor import TaskExecutor
        r = TaskExecutor._normalize_result(None)
        assert r.summary == "完成"

    def test_construction_strips_none_deps(self):
        from copixiv.tasks.manager import TaskManagerSystem

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
        from copixiv.tasks.manager import TaskManagerSystem

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

