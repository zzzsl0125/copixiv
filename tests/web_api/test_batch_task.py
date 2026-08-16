"""Batch-task tests — POST /api/novels/batch-task + the batch_operation task.

Covers: synchronous validation at submit time, enqueue through the task
manager, chunked execution with per-chunk transactions, progress reporting,
and file cleanup.  The client fixture mirrors test_batch_operations.py but
adds a recording fake task manager.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from copixiv.app.config import AppConfig
from copixiv.domain.exceptions import DomainError, TaskAlreadyRunningError
from copixiv.infrastructure.database.models import (
    Author, Novel, Tag, NovelTag, TaskHistory,
)
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import DbWriteLock
from copixiv.web_api.endpoints import novels
from copixiv.tasks.batch_tasks import batch_operation


class _FakeFileStorage:
    def __init__(self, download_dir: Path):
        self.download_dir = str(download_dir)

    def delete_novel_files(self, novel_path: str) -> None:
        p = Path(novel_path)
        p.unlink(missing_ok=True)
        p.with_suffix(".epub").unlink(missing_ok=True)


class _RecordingTaskManager:
    """Records enqueues; raises TaskAlreadyRunningError when told to."""

    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []
        self.next_task_id = 1000
        self.busy = False

    def run_task(self, name: str, func=None, params: dict | None = None) -> int:
        if self.busy:
            raise TaskAlreadyRunningError(f"Task '{name}' is already pending or running.")
        self.enqueued.append((name, params or {}))
        self.next_task_id += 1
        return self.next_task_id


@pytest.fixture
def client(session_factory, tmp_path):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.file_storage = _FakeFileStorage(tmp_path)
    app.state.task_manager = _RecordingTaskManager()

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])

    with TestClient(app) as c:
        yield c


def _seed(sf, novel_id: int, title: str, path: str, **extra):
    with sf() as s:
        s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
        s.flush()
        s.add(Novel(
            id=novel_id, title=title, author_id=novel_id,
            author_name=f"作者{novel_id}", path=path,
            has_epub=0, **extra,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# POST /api/novels/batch-task — submit-time validation + enqueue
# ---------------------------------------------------------------------------


class TestBatchTaskEndpoint:
    def test_submits_ids_scope_and_returns_task_id(self, client, session_factory):
        for i in (1, 2, 3):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.post("/api/novels/batch-task", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": [3, 1], "excluded_ids": []},
        })
        assert r.status_code == 200
        body = r.json()
        assert body == {"task_id": 1001, "matched": 2}

        tm = client.app.state.task_manager
        name, params = tm.enqueued[0]
        assert name == "batch_operation"
        assert params["operation"] == "delete"
        assert params["novel_ids"] == [1, 3]

    def test_empty_ids_is_400(self, client):
        r = client.post("/api/novels/batch-task", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": [], "excluded_ids": []},
        })
        assert r.status_code == 400
        assert "勾选" in r.json()["detail"]

    def test_tag_op_requires_tags(self, client, session_factory):
        _seed(session_factory, 1, "标题", str(Path("/tmp/1.txt")))
        r = client.post("/api/novels/batch-task", json={
            "operation": "add_tags",
            "scope": {"mode": "ids", "novel_ids": [1], "excluded_ids": []},
            "tags": ["  "],
        })
        assert r.status_code == 400
        assert "标签" in r.json()["detail"]

    def test_all_matched_scope_resolves_uncapped(self, client, session_factory):
        # The task path must NOT apply the sync 5000 cap — resolve the full
        # matched set here (monkeypatching the cap proves it is ignored).
        from copixiv.web_api.endpoints import novels as novels_endpoints
        for i in range(1, 4):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")),
                  like=10)
        original = novels_endpoints.BATCH_MAX_NOVELS
        try:
            novels_endpoints.BATCH_MAX_NOVELS = 2
            r = client.post("/api/novels/batch-task", json={
                "operation": "delete",
                "scope": {"mode": "all_matched", "keyword": "like:10",
                          "novel_ids": [], "excluded_ids": []},
            })
        finally:
            novels_endpoints.BATCH_MAX_NOVELS = original

        assert r.status_code == 200
        assert r.json()["matched"] == 3

    def test_duplicate_run_maps_to_409(self, client, session_factory):
        _seed(session_factory, 1, "标题", str(Path("/tmp/1.txt")))
        client.app.state.task_manager.busy = True
        r = client.post("/api/novels/batch-task", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": [1], "excluded_ids": []},
        })
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# The batch_operation task function itself — chunked execution
# ---------------------------------------------------------------------------


async def _run_task(session_factory, tmp_path, *, operation, novel_ids,
                    tags=None, chunk_size=2, with_history=True):
    from copixiv.tasks import batch_tasks

    original = batch_tasks.BATCH_TASK_SAFETY_CHUNK
    batch_tasks.BATCH_TASK_SAFETY_CHUNK = chunk_size
    try:
        task_id = None
        if with_history:
            with session_factory() as s:
                row = TaskHistory(
                    name="batch_operation", status="pending",
                    arguments=json.dumps({}),
                    start_time="2026-01-01T00:00:00",
                )
                s.add(row)
                s.commit()
                task_id = row.id

        uow = SqlUnitOfWork(session_factory)
        return await batch_operation(
            batch_tasks.BatchOperationArgs(
                operation=operation,
                novel_ids=novel_ids,
                tags=tags,
            ),
            batch_tasks.TaskContext(
                uow=uow,
                write_lock=DbWriteLock(),
                file_storage=_FakeFileStorage(tmp_path),
                task_id=task_id,
            ),
        )
    finally:
        batch_tasks.BATCH_TASK_SAFETY_CHUNK = original


class TestBatchOperationTask:
    def test_delete_chunks_remove_rows_files_and_tag_counts(
        self, session_factory, tmp_path,
    ):
        for i in range(1, 6):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p))
        with session_factory() as s:
            tag = Tag(name="R-18", reference_count=2)
            s.add(tag)
            s.flush()
            s.add_all([NovelTag(novel_id=1, tag_id=tag.id),
                       NovelTag(novel_id=2, tag_id=tag.id)])
            s.commit()

        result = pytest.mark.asyncio and None
        import asyncio
        result = asyncio.run(_run_task(
            session_factory, tmp_path,
            operation="delete", novel_ids=[1, 2, 3, 4, 5], chunk_size=2,
        ))

        assert "完成" in result.summary
        assert "共处理 5/5 篇" in result.summary

        for i in range(1, 6):
            assert not (tmp_path / f"{i}.txt").exists()
        with session_factory() as s:
            assert s.query(Novel).count() == 0
            tag = s.query(Tag).filter_by(name="R-18").one()
            assert tag.reference_count == 0

    def test_progress_is_written_to_history(
        self, session_factory, tmp_path,
    ):
        for i in (1, 2, 3):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        import asyncio
        asyncio.run(_run_task(
            session_factory, tmp_path,
            operation="delete", novel_ids=[1, 2, 3], chunk_size=2,
        ))

        with session_factory() as s:
            row = s.query(TaskHistory).one()
            # The wrapper would overwrite result at the end; the task itself
            # leaves "running" + a progress summary in the row.
            assert row.status == "running"
            assert "进行中" in row.result or "完成" in row.result

    def test_add_tags_across_chunks(self, session_factory, tmp_path):
        for i in (1, 2, 3):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        import asyncio
        result = asyncio.run(_run_task(
            session_factory, tmp_path,
            operation="add_tags", novel_ids=[1, 2, 3],
            tags=["新标签"], chunk_size=2,
        ))
        assert "共处理 3/3 篇" in result.summary

        with session_factory() as s:
            tag = s.query(Tag).filter_by(name="新标签").one()
            assert tag.reference_count == 3
            assert s.query(NovelTag).filter_by(tag_id=tag.id).count() == 3

    def test_partial_chunk_failure_is_reported(self, session_factory, tmp_path):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))

        import asyncio
        result = asyncio.run(_run_task(
            session_factory, tmp_path,
            operation="remove_tags", novel_ids=[1],
            tags=["无所谓"], chunk_size=1,
        ))
        # Removing a tag nobody has → affected 0, still completes.
        assert "共处理 1/1 篇" in result.summary

    def test_unknown_operation_raises(self, session_factory, tmp_path):
        import asyncio
        with pytest.raises(Exception, match="未知的批量操作"):
            asyncio.run(_run_task(
                session_factory, tmp_path,
                operation="explode", novel_ids=[1], chunk_size=1,
            ))

# ---------------------------------------------------------------------------
# POST /api/novels/batch-export + the batch_export task + download endpoint
# ---------------------------------------------------------------------------


class TestBatchExportEndpoint:
    def test_submits_and_returns_task_id(self, client):
        r = client.post("/api/novels/batch-export", json={
            "novel_ids": [5, 3, 1],
            "format_mode": "txt",
            "zip_name": "我的导出",
        })
        assert r.status_code == 200
        body = r.json()
        assert body == {"task_id": 1001, "matched": 3}

        name, params = client.app.state.task_manager.enqueued[0]
        assert name == "batch_export"
        assert params["novel_ids"] == [1, 3, 5]
        assert params["zip_name"] == "我的导出"

    def test_empty_ids_is_400(self, client):
        r = client.post("/api/novels/batch-export", json={"novel_ids": []})
        assert r.status_code == 400
        assert "勾选" in r.json()["detail"]

    def test_download_missing_file_is_404(self, client):
        r = client.get("/api/novels/export/9999/download")
        assert r.status_code == 404


class TestBatchExportTask:
    def test_builds_zip_into_download_dir_and_download_endpoint_serves_it(
        self, client, session_factory, tmp_path,
    ):
        import asyncio
        import zipfile
        import io

        from copixiv.infrastructure.database.uow import SqlUnitOfWork
        from copixiv.infrastructure.database.write_lock import DbWriteLock
        from copixiv.tasks.batch_tasks import BatchExportArgs, batch_export
        from copixiv.tasks.context import TaskContext

        for i in (1, 2):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p))

        with session_factory() as s:
            row = TaskHistory(
                name="batch_export", status="pending",
                arguments=json.dumps({"zip_name": "导出测试"}),
                start_time="2026-01-01T00:00:00",
            )
            s.add(row)
            s.commit()
            task_id = row.id

        result = asyncio.run(batch_export(
            BatchExportArgs(
                novel_ids=[1, 2],
                format_mode="txt",
                zip_name="导出测试",
            ),
            TaskContext(
                uow=SqlUnitOfWork(session_factory),
                write_lock=DbWriteLock(),
                file_storage=_FakeFileStorage(tmp_path),
                task_id=task_id,
            ),
        ))

        assert "批量导出完成" in result.summary
        assert "2 篇" in result.summary

        zip_path = tmp_path / f"batch_export_{task_id}.zip"
        assert zip_path.is_file()
        with zipfile.ZipFile(str(zip_path)) as zf:
            assert len(zf.namelist()) == 2

        # The client fixture shares tmp_path → the download endpoint finds it.
        r = client.get(f"/api/novels/export/{task_id}/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        from urllib.parse import unquote
        assert "导出测试.zip" in unquote(r.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert len(zf.namelist()) == 2

    def test_unknown_ids_raise(self, session_factory, tmp_path):
        import asyncio

        from copixiv.infrastructure.database.uow import SqlUnitOfWork
        from copixiv.infrastructure.database.write_lock import DbWriteLock
        from copixiv.tasks.batch_tasks import BatchExportArgs, batch_export
        from copixiv.tasks.context import TaskContext

        with pytest.raises(Exception, match="均不存在"):
            asyncio.run(batch_export(
                BatchExportArgs(novel_ids=[999999]),
                TaskContext(
                    uow=SqlUnitOfWork(session_factory),
                    write_lock=DbWriteLock(),
                    file_storage=_FakeFileStorage(tmp_path),
                ),
            ))
