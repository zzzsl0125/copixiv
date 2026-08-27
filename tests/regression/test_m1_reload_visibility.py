"""M1 复现：计划任务 CRUD 后 reload_cron_jobs 在事务提交前执行 → 调度器看不到变更。

修复目标（期望行为）：
- POST /api/tasks/scheduled 响应完成后，新任务的 cron job 必须已注册进调度器。
- DELETE 后，对应 cron job 必须已移除。

当前实现（bug）：端点内同步调用 reload_cron_jobs()，而请求事务在依赖
teardown（handler 返回后）才提交，reload 用另一连接读不到未提交数据。
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from copixiv.app import _domain_error_http_status
from copixiv.core.exceptions import DomainError
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from copixiv.tasks.kernel import TaskManagerSystem
from copixiv.tasks import api as tasks_endpoint


# file_session_factory comes from tests/conftest.py


def _cron_job_ids(tm: TaskManagerSystem) -> list[str]:
    return [j.id for j in tm.scheduler.get_jobs() if j.id.startswith("cron_")]


# ---------------------------------------------------------------------------
# 机制级复现（无 HTTP 层）：flush 未提交时 reload 看不到 → 期望：能看到
# ---------------------------------------------------------------------------


def test_reload_sees_pending_insert_after_commit_only(file_session_factory):
    """复现原始机制：未提交时 reload 看不到新任务；提交后能看到。

    期望行为：只要 reload 发生在 commit 之后（修复后的时序），
    调度器一定包含新任务 —— 本测试固定「commit 后可见」这一不变量。
    """
    factory = file_session_factory

    async def scenario():
        tm = TaskManagerSystem(session_factory=factory, client=None)
        tm.start()
        try:
            with factory() as s:
                repo = SQLAlchemyTaskRepository(s)
                await repo.create_scheduled({
                    "name": "t", "task": "check_epub", "cron": "*/5 * * * *",
                    "params": "{}", "is_enabled": True, "sort_index": 0,
                })
                s.flush()
                tm.reload_cron_jobs()
                # 未提交：按修复后语义这里也应注册（修复方式是把 reload 挪到提交后）
                assert _cron_job_ids(tm) == [], "提交前 reload 不应读得到（WAL 快照）"
                s.commit()
            tm.reload_cron_jobs()
            assert _cron_job_ids(tm) == ["cron_1"]
        finally:
            tm.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 端点级复现：POST /api/tasks/scheduled 后调度器必须已有 cron job
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(file_session_factory):
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient

    factory = file_session_factory

    tm = TaskManagerSystem(session_factory=factory, client=None)

    @asynccontextmanager
    async def lifespan(app):
        tm.start()
        yield
        tm.stop()

    app = FastAPI(title="test", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(status_code=_domain_error_http_status(exc),
                            content={"detail": exc.detail})

    app.include_router(tasks_endpoint.router, prefix="/api/tasks", tags=["tasks"])

    app.state.task_manager = tm
    app.state.session_factory = factory

    with TestClient(app) as c:
        yield c, tm


def test_create_scheduled_registers_cron_job_after_response(client):
    c, tm = client
    r = c.post("/api/tasks/scheduled", json={
        "name": "每小时任务", "task": "check_epub",
        "cron": "0 * * * *", "is_enabled": True,
    })
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]

    # 期望：响应完成后（事务已提交 + reload 已执行）调度器有 cron_<id>
    assert f"cron_{task_id}" in _cron_job_ids(tm)


def test_delete_scheduled_removes_cron_job_after_response(client):
    c, tm = client
    r = c.post("/api/tasks/scheduled", json={
        "name": "删除我", "task": "check_epub",
        "cron": "0 * * * *", "is_enabled": True,
    })
    task_id = r.json()["id"]
    assert f"cron_{task_id}" in _cron_job_ids(tm)

    r = c.delete(f"/api/tasks/scheduled/{task_id}")
    assert r.status_code == 200

    # 期望：删除后 reload 在提交后执行 → job 已移除
    assert f"cron_{task_id}" not in _cron_job_ids(tm)
