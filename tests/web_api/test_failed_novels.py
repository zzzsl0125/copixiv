"""Endpoint tests for the failed-novel ledger API.

Minimal FastAPI app wired exactly like ``container.create_app()`` (router
+ DomainError handler + app.state deps), with an in-memory SQLite engine
and a fake task manager that records ``run_task`` calls.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import pytest

from copixiv.domain.exceptions import DomainError
from copixiv.infrastructure.database.models import FailedNovel
from copixiv.web_api.endpoints import failed_novels


class _FakeTaskManager:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run_task(self, name: str, params: dict | None = None) -> int:
        self.calls.append((name, params or {}))
        return len(self.calls)


@pytest.fixture
def client(session_factory):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = None
    app.state.task_manager = _FakeTaskManager()

    app.include_router(
        failed_novels.router, prefix="/api/failed-novels",
        tags=["failed-novels"],
    )

    with TestClient(app) as c:
        yield c


def _seed(sf, novel_id: int, title: str | None, failed_times: int = 1,
          last_failed_at: str | None = "2026-08-19 19:54:58"):
    with sf() as s:
        s.add(FailedNovel(
            novel_id=novel_id,
            failure_type="download",
            error_message=f"error-{novel_id}",
            failed_times=failed_times,
            title=title,
            last_failed_at=last_failed_at,
        ))
        s.commit()


class TestFailedNovelList:
    def test_empty(self, client):
        r = client.get("/api/failed-novels/")
        assert r.status_code == 200
        body = r.json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 100}

    def test_list_shape_and_ordering(self, client, session_factory):
        _seed(session_factory, 2, "标题B", last_failed_at="2026-08-19 20:00:00")
        _seed(session_factory, 1, None, last_failed_at=None)  # 存量旧记录
        r = client.get("/api/failed-novels/")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert [i["novel_id"] for i in body["items"]] == [2, 1]
        first = body["items"][0]
        assert first["title"] == "标题B"
        assert first["failed_times"] == 1
        assert first["error_message"] == "error-2"
        assert first["last_failed_at"] == "2026-08-19 20:00:00"
        # 旧记录：标题与时间为 null
        assert body["items"][1]["title"] is None
        assert body["items"][1]["last_failed_at"] is None

    def test_pagination(self, client, session_factory):
        for i in range(1, 6):
            _seed(session_factory, i, f"标题{i}",
                  last_failed_at=f"2026-08-19 20:0{i}:00")
        r = client.get("/api/failed-novels/", params={"offset": 0, "limit": 2})
        assert [i["novel_id"] for i in r.json()["items"]] == [5, 4]
        r = client.get("/api/failed-novels/", params={"offset": 2, "limit": 2})
        assert [i["novel_id"] for i in r.json()["items"]] == [3, 2]

    def test_count(self, client, session_factory):
        _seed(session_factory, 1, "a")
        _seed(session_factory, 2, "b")
        r = client.get("/api/failed-novels/count")
        assert r.status_code == 200
        assert r.json() == {"count": 2}

    def test_not_found_family_sorted_last(self, client, session_factory):
        """Page-not-found 家族排最后，其余按时间倒序（用户排序要求）。"""
        with session_factory() as s:
            s.add(FailedNovel(
                novel_id=1, failure_type="download",
                error_message="EPUB 生成失败: novel 1", failed_times=1,
                title="可修复", last_failed_at="2026-08-19 20:55:00",
            ))
            s.add(FailedNovel(
                novel_id=2, failure_type="download",
                error_message="webview_novel 返回空", failed_times=1,
                title=None, last_failed_at="2026-08-19 20:56:00",
            ))
            s.add(FailedNovel(
                novel_id=3, failure_type="download",
                error_message="Page not found", failed_times=1,
                title=None, last_failed_at="2026-08-19 20:57:00",
            ))
            s.commit()

        body = client.get("/api/failed-novels/").json()
        assert [i["novel_id"] for i in body["items"]] == [1, 3, 2]


class TestFailedNovelDelete:
    def test_delete_one(self, client, session_factory):
        _seed(session_factory, 1, "a")
        r = client.delete("/api/failed-novels/1")
        assert r.status_code == 204
        with session_factory() as s:
            assert s.get(FailedNovel, 1) is None

    def test_clear_all(self, client, session_factory):
        _seed(session_factory, 1, "a")
        _seed(session_factory, 2, "b")
        r = client.delete("/api/failed-novels/")
        assert r.status_code == 204
        with session_factory() as s:
            assert s.query(FailedNovel).count() == 0


class TestFailedNovelResetCount:
    """重置计数：记录保留、计数归零、解封自动重试。"""

    def test_reset_one_keeps_record(self, client, session_factory):
        _seed(session_factory, 1, "标题A", failed_times=4)
        r = client.post("/api/failed-novels/1/reset-count")
        assert r.status_code == 204
        with session_factory() as s:
            row = s.get(FailedNovel, 1)
            assert row is not None
            assert row.failed_times == 0
            assert row.title == "标题A"

    def test_reset_all_keeps_records(self, client, session_factory):
        _seed(session_factory, 1, "a", failed_times=3)
        _seed(session_factory, 2, "b", failed_times=25)
        r = client.post("/api/failed-novels/reset-count")
        assert r.status_code == 204
        with session_factory() as s:
            assert s.query(FailedNovel).count() == 2
            for row in s.query(FailedNovel):
                assert row.failed_times == 0


class TestFailedNovelRetry:
    def test_retry_enqueues_task(self, client, session_factory):
        _seed(session_factory, 1, "a")
        _seed(session_factory, 2, "b")
        r = client.post(
            "/api/failed-novels/retry",
            json={"novel_ids": [2, 2, 1]},  # 去重
        )
        assert r.status_code == 200
        body = r.json()
        assert body["matched"] == 2
        manager = client.app.state.task_manager
        assert manager.calls == [("failed_retry", {"novel_ids": [2, 1]})]

    def test_retry_empty_rejected(self, client):
        r = client.post("/api/failed-novels/retry", json={"novel_ids": []})
        assert r.status_code == 422 or r.status_code == 400

    def test_retry_too_many_rejected(self, client):
        r = client.post(
            "/api/failed-novels/retry",
            json={"novel_ids": list(range(501))},
        )
        assert r.status_code == 400
        assert "500" in r.json()["detail"]

    def test_retry_all_enqueues_whole_ledger(self, client, session_factory):
        """retry-all 以整本台账为载荷——不依赖客户端分页状态。"""
        _seed(session_factory, 1, "a")
        _seed(session_factory, 2, "b")
        _seed(session_factory, 3, "c")
        r = client.post("/api/failed-novels/retry-all")
        assert r.status_code == 200
        body = r.json()
        assert body["matched"] == 3
        manager = client.app.state.task_manager
        assert manager.calls == [("failed_retry", {"novel_ids": [1, 2, 3]})]

    def test_retry_all_empty_rejected(self, client):
        r = client.post("/api/failed-novels/retry-all")
        assert r.status_code == 400
        assert "没有失败记录" in r.json()["detail"]
