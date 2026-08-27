"""M7 复现：任务重复触发无防护 + 僵尸任务无恢复。

期望：
- 同一任务名存在 pending/running 历史时，run_task 拒绝再次入队
  （抛 TaskAlreadyRunningError，端点映射为 409）。
- 启动时（或显式恢复调用）stale 的 pending/running 历史被标记 interrupted。
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from copixiv.app import _domain_error_http_status
from copixiv.core.exceptions import DomainError, TaskAlreadyRunningError
from copixiv.db.models import TaskHistory
from copixiv.tasks.kernel import TaskManagerSystem
from copixiv.tasks import api as tasks_endpoint


@pytest.fixture()
def factory(file_session_factory):
    return file_session_factory


async def _noop_task(**kwargs):
    return "ok"


def _mk_tm(factory):
    return TaskManagerSystem(session_factory=factory, client=None)


async def test_duplicate_run_task_rejected_when_pending(factory):
    """期望：同任务已有 pending 行时，第二次入队被拒绝。

    依赖 DB 的唯一索引（``task_func`` 部分唯一索引），而非进程内守卫。
    不 start()：入队的 one-shot 任务永不执行，pending 行保持存在，第二次
    入队才能确定性地命中重复运行约束。
    """
    tm = _mk_tm(factory)
    try:
        tm.run_task("novel_fetch", _noop_task, {"id": 1})
        with pytest.raises(TaskAlreadyRunningError, match="pending|running|进行中|重复|已存在"):
            tm.run_task("novel_fetch", _noop_task, {"id": 1})
    finally:
        tm.stop()


async def test_duplicate_run_task_rejected_when_running(factory):
    """期望：running 状态同样阻止重复入队。

    依赖 DB 的唯一索引（``task_func`` 部分唯一索引）。必须先带
    ``task_func`` 插入 running 行——否则 NULL 不受索引约束。
    不 start()：start 时的僵尸恢复会把 pending/running 行标为 interrupted。
    """
    tm = _mk_tm(factory)
    try:
        with factory() as s:
            s.add(TaskHistory(name="author_fetch", task_func="author_fetch",
                              status="running",
                              start_time="2026-01-01T00:00:00"))
            s.commit()

        with pytest.raises(TaskAlreadyRunningError, match="running|pending|进行中|重复|已存在"):
            tm.run_task("author_fetch", _noop_task, {"author_id": 1})
    finally:
        tm.stop()


async def test_duplicate_run_scheduled_same_func_different_display_name(factory):
    """同一函数、两个不同显示名的 scheduled 条目：第二个 run_scheduled 拒绝。

    去重键是函数名（``task_func`` 列）而非显示名（``name`` 列）——第一个
    pending 悬着，第二个入队应命中唯一索引约束。
    """
    tm = _mk_tm(factory)
    try:
        tm.run_scheduled("显示名A", "check_epub", {"a": 1})
        with pytest.raises(TaskAlreadyRunningError, match="pending|running|进行中|重复|已存在"):
            tm.run_scheduled("显示名B", "check_epub", {"a": 2})
    finally:
        tm.stop()


async def test_stale_tasks_recovered_on_start(factory):
    """期望：启动后 stale 的 pending/running 行被标记为 interrupted。"""
    with factory() as s:
        s.add_all([
            TaskHistory(name="novel_search", status="pending",
                        start_time="2026-01-01T00:00:00"),
            TaskHistory(name="novel_ranking", status="running",
                        start_time="2026-01-01T00:00:00"),
            TaskHistory(name="check_epub", status="success",
                        start_time="2026-01-01T00:00:00"),
        ])
        s.commit()

    tm = _mk_tm(factory)
    tm.start()
    try:
        with factory() as s:
            rows = {
                h.name: h.status
                for h in s.execute(select(TaskHistory)).scalars().all()
            }
        assert rows["novel_search"] == "interrupted"
        assert rows["novel_ranking"] == "interrupted"
        assert rows["check_epub"] == "success"  # 已终结的不动
    finally:
        tm.stop()


# ---------------------------------------------------------------------------
# 端点级契约：POST /api/tasks/scheduled/{id}/run 重复运行 → 409
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(factory):
    # Scheduler 故意不 start()：入队的 one-shot job 永不执行，pending 行
    # 保持存在，第二次 /run 才能确定性地命中重复运行守卫。
    tm = TaskManagerSystem(session_factory=factory, client=None)

    app = FastAPI(title="test")

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(status_code=_domain_error_http_status(exc),
                            content={"detail": exc.detail})

    app.include_router(tasks_endpoint.router, prefix="/api/tasks", tags=["tasks"])
    app.state.task_manager = tm
    app.state.session_factory = factory

    with TestClient(app) as c:
        yield c


def test_run_scheduled_task_twice_second_is_409(client):
    r = client.post("/api/tasks/scheduled", json={
        "name": "重复运行任务", "task": "check_epub",
        "cron": "0 * * * *", "is_enabled": True,
    })
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]

    r = client.post(f"/api/tasks/scheduled/{task_id}/run")
    assert r.status_code == 200

    r = client.post(f"/api/tasks/scheduled/{task_id}/run")
    assert r.status_code == 409
    assert "detail" in r.json()
