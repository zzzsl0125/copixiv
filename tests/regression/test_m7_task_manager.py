"""M7 复现：任务重复触发无防护 + 僵尸任务无恢复。

期望：
- 同一任务名存在 pending/running 历史时，run_task 拒绝再次入队（抛 409 异常）。
- 启动时（或显式恢复调用）stale 的 pending/running 历史被标记 interrupted。
"""

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from copixiv.infrastructure.database.models import Base, TaskHistory
from copixiv.tasks.manager import TaskManagerSystem


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


async def _noop_task(**kwargs):
    return "ok"


def _mk_tm(factory):
    return TaskManagerSystem(session_factory=factory, client=None)


def test_duplicate_run_task_rejected_when_pending(factory):
    """期望：同任务名已有 pending 行时，第二次入队被拒绝。"""
    async def scenario():
        tm = _mk_tm(factory)
        tm.start()
        try:
            tm.run_task("novel_fetch", _noop_task, {"id": 1})
            with pytest.raises(Exception, match="running|pending|进行中|重复"):
                tm.run_task("novel_fetch", _noop_task, {"id": 1})
        finally:
            tm.stop()

    asyncio.run(scenario())


def test_duplicate_run_task_rejected_when_running(factory):
    """期望：running 状态同样阻止重复入队。

    注意：running 行在 tm.start() **之后**插入——启动时的僵尸恢复会把
    启动前遗留的 pending/running 行标记为 interrupted（这是期望行为）。
    """
    async def scenario():
        tm = _mk_tm(factory)
        tm.start()
        try:
            with factory() as s:
                s.add(TaskHistory(name="author_fetch", status="running",
                                  start_time="2026-01-01T00:00:00"))
                s.commit()

            with pytest.raises(Exception, match="running|pending|进行中|重复"):
                tm.run_task("author_fetch", _noop_task, {"author_id": 1})
        finally:
            tm.stop()

    asyncio.run(scenario())


def test_stale_tasks_recovered_on_start(factory):
    """期望：启动后 stale 的 pending/running 行被标记为 interrupted。"""
    async def scenario():
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

    asyncio.run(scenario())
