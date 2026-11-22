"""Layer 2 DB tests: TaskManagerSystem queue lanes, stale recovery, invalid args, and shutdown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from copixiv.core.models import EpubStatus, TaskResult
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.use_cases.refetch_missing import find_refetch_candidates
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks import registry
from copixiv.tasks.batch import BatchExportArgs, batch_export
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.manager import TaskManagerSystem


class _EchoArgs(BaseModel):
    value: str = "hi"


@pytest.mark.asyncio
async def test_task_manager_recovers_stale_runs_both_lanes_and_records_invalid_rows(
    committed_session_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    executed: list[tuple[str, str]] = []
    done_event = asyncio.Event()

    @registry.register("_test_fetch_lane", args=_EchoArgs, lane="fetch")
    async def _fetch_task(args: _EchoArgs, ctx: TaskContext):
        del ctx
        executed.append(("fetch", args.value))
        if len(executed) >= 2:
            done_event.set()
        return TaskResult(summary=f"fetch:{args.value}")

    @registry.register("_test_general_lane", args=_EchoArgs, lane="general")
    async def _general_task(args: _EchoArgs, ctx: TaskContext):
        del ctx
        executed.append(("general", args.value))
        if len(executed) >= 2:
            done_event.set()
        return TaskResult(summary=f"general:{args.value}")

    monkeypatch.setitem(registry._registry, "_test_fetch_lane", registry._registry["_test_fetch_lane"])
    monkeypatch.setitem(registry._registry, "_test_general_lane", registry._registry["_test_general_lane"])

    # Seed two stale rows (should become interrupted on start)
    now = datetime.now(UTC)
    with committed_session_factory() as session:
        stale_pending = models.TaskHistory(
            name="stale_p",
            task_func="_test_general_lane",
            arguments={},
            status="pending",
            start_time=now,
        )
        stale_running = models.TaskHistory(
            name="stale_r",
            task_func="_test_fetch_lane",
            arguments={},
            status="running",
            start_time=now,
        )
        session.add_all([stale_pending, stale_running])
        session.commit()
        stale_ids = [stale_pending.id, stale_running.id]

    mgr = TaskManagerSystem(committed_session_factory)
    await mgr.start()
    try:
        with committed_session_factory() as session:
            for sid in stale_ids:
                row = session.get(models.TaskHistory, sid)
                assert row is not None
                assert row.status == "interrupted"

        # Insert a non-dict arguments row and an unknown func row after start, then enqueue valid tasks
        with committed_session_factory() as session:
            bad_args = models.TaskHistory(
                name="bad_args",
                task_func="_test_general_lane",
                arguments=["not-a-dict"],  # type: ignore[arg-type]
                status="pending",
                start_time=now,
            )
            unknown_fn = models.TaskHistory(
                name="unknown_fn",
                task_func="no_such_task_xyz",
                arguments={},
                status="pending",
                start_time=now,
            )
            session.add_all([bad_args, unknown_fn])
            session.commit()
            bad_args_id, unknown_id = bad_args.id, unknown_fn.id

        fetch_id = mgr.run_task("_test_fetch_lane", {"value": "A"})
        general_id = mgr.run_task("_test_general_lane", {"value": "B"})

        await asyncio.wait_for(done_event.wait(), timeout=5.0)
        # Wait briefly until both rows persist terminal status
        for _ in range(50):
            with committed_session_factory() as session:
                statuses = [
                    session.get(models.TaskHistory, tid).status
                    for tid in (bad_args_id, unknown_id, fetch_id, general_id)
                ]
                if all(s in ("success", "failed") for s in statuses):
                    break
            await asyncio.sleep(0.05)

        with committed_session_factory() as session:
            assert session.get(models.TaskHistory, bad_args_id).status == "failed"
            assert session.get(models.TaskHistory, unknown_id).status == "failed"
            r_fetch = session.get(models.TaskHistory, fetch_id)
            r_general = session.get(models.TaskHistory, general_id)
            assert r_fetch.status == "success"
            assert r_fetch.result["summary"] == "fetch:A"
            assert r_general.status == "success"
            assert r_general.result["summary"] == "general:B"
    finally:
        await mgr.stop()
        assert mgr._started is False


@pytest.mark.asyncio
async def test_refetch_pre_limit_and_batch_export_progress(
    committed_session_factory,
    tmp_path: Path,
):
    now = datetime.now(UTC)

    # 1. refetch._candidates must filter failed rows BEFORE applying LIMIT
    #    (3 skipped rows with ids 1..3 + 1 eligible row with id 4 -> limit=1 returns [4], not [])
    with committed_session_factory() as session:
        session.add_all(
            [
                models.Novel(
                    id=1,
                    title="S1",
                    path=str(tmp_path / "1.txt"),
                    has_epub=EpubStatus.MISSING_SOURCE,
                    source="pixiv",
                ),
                models.Novel(
                    id=2,
                    title="S2",
                    path=str(tmp_path / "2.txt"),
                    has_epub=EpubStatus.MISSING_SOURCE,
                    source="pixiv",
                ),
                models.Novel(
                    id=3,
                    title="S3",
                    path=str(tmp_path / "3.txt"),
                    has_epub=EpubStatus.MISSING_SOURCE,
                    source="pixiv",
                ),
                models.Novel(
                    id=4,
                    title="OK4",
                    path=str(tmp_path / "4.txt"),
                    has_epub=EpubStatus.MISSING_SOURCE,
                    source="pixiv",
                ),
                models.FailedNovel(novel_id=1, error_message="dead", failed_times=3, last_failed_at=now),
                models.FailedNovel(novel_id=2, error_message="dead", failed_times=3, last_failed_at=now),
                models.FailedNovel(novel_id=3, error_message="recent", failed_times=1, last_failed_at=now),
            ]
        )
        session.commit()

    with committed_session_factory() as session:
        assert find_refetch_candidates(session, limit=1, skip_cutoff=now.replace(year=2020)) == [4]

    # 2. batch_export reports progress via isolated child UoW & writes export zip
    storage = FileStorage(str(tmp_path))
    p_ok = tmp_path / "0000" / "OK4_4.txt"
    p_ok.parent.mkdir(parents=True, exist_ok=True)
    p_ok.write_text("hello export", encoding="utf-8")
    with committed_session_factory() as session:
        session.get(models.Novel, 4).path = str(p_ok)
        th_export = models.TaskHistory(name="exp", task_func="batch_export", status="running", start_time=now)
        session.add(th_export)
        session.commit()
        exp_id = th_export.id

    ctx_exp = TaskContext(
        uow=SqlUnitOfWork(committed_session_factory),
        session_factory=committed_session_factory,
        file_storage=storage,
        task_id=exp_id,
    )
    res_exp = await batch_export(BatchExportArgs(novel_ids=[4], zip_name="top.zip"), ctx_exp)
    assert "批量导出完成" in res_exp.summary
    assert (tmp_path / f"batch_export_{exp_id}.zip").is_file()
    with committed_session_factory() as session:
        assert session.get(models.TaskHistory, exp_id).progress is not None

    # 3. batch_export raises NotFoundError and leaves no 0-byte zip when all files are missing
    from copixiv.core.exceptions import NotFoundError

    p_ok.unlink()
    with committed_session_factory() as session:
        th_empty = models.TaskHistory(
            name="exp_empty", task_func="batch_export", status="running", start_time=now
        )
        session.add(th_empty)
        session.commit()
        empty_id = th_empty.id

    ctx_empty = TaskContext(
        uow=SqlUnitOfWork(committed_session_factory),
        session_factory=committed_session_factory,
        file_storage=storage,
        task_id=empty_id,
    )
    with pytest.raises(NotFoundError, match="未找到可下载的有效文件"):
        await batch_export(BatchExportArgs(novel_ids=[4], zip_name="empty.zip"), ctx_empty)
    assert not (tmp_path / f"batch_export_{empty_id}.zip").exists()


@pytest.mark.asyncio
async def test_task_manager_cancel_pending_running_and_get_export_info(
    committed_session_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    started = asyncio.Event()
    follow_done = asyncio.Event()

    @registry.register("_test_cancel_slow", lane="general", notify=False)
    async def _slow(ctx: TaskContext):
        del ctx
        started.set()
        await asyncio.sleep(30)
        return TaskResult(summary="never")

    @registry.register("_test_cancel_followup", lane="general", notify=False)
    async def _followup(ctx: TaskContext):
        del ctx
        follow_done.set()
        return TaskResult(summary="followup-ok")

    monkeypatch.setitem(registry._registry, "_test_cancel_slow", registry._registry["_test_cancel_slow"])
    monkeypatch.setitem(
        registry._registry, "_test_cancel_followup", registry._registry["_test_cancel_followup"]
    )

    now = datetime.now(UTC)
    with committed_session_factory() as session:
        exp_row = models.TaskHistory(
            name="exp",
            task_func="batch_export",
            arguments={"zip_name": "top.zip"},
            status="success",
            start_time=now,
        )
        pending_row = models.TaskHistory(
            name="pending_cancel",
            task_func="_test_cancel_followup",
            arguments={},
            status="pending",
            start_time=now,
        )
        session.add_all([exp_row, pending_row])
        session.commit()
        exp_id = exp_row.id
        pending_id = pending_row.id

    mgr = TaskManagerSystem(committed_session_factory)

    # 1. get_export_info parses zip_name & returns None for unknown ID
    info = mgr.get_export_info(exp_id)
    assert info is not None
    assert info.zip_name == "top.zip"
    assert mgr.get_export_info(999_999_999) is None

    # 2. cancel_task cancels a pending DB row before worker picks it up
    assert mgr.cancel_task(pending_id) == "cancelled"
    with committed_session_factory() as session:
        assert session.get(models.TaskHistory, pending_id).status == "cancelled"

    # 3. cancel_task interrupts a live running task without breaking the lane worker loop
    await mgr.start()
    try:
        slow_id = mgr.run_task("_test_cancel_slow")
        await asyncio.wait_for(started.wait(), timeout=5.0)
        assert mgr.cancel_task(slow_id) == "cancelling"

        followup_id = mgr.run_task("_test_cancel_followup")
        await asyncio.wait_for(follow_done.wait(), timeout=5.0)

        for _ in range(50):
            with committed_session_factory() as session:
                s_slow = session.get(models.TaskHistory, slow_id).status
                s_next = session.get(models.TaskHistory, followup_id).status
                if s_slow == "cancelled" and s_next == "success":
                    break
            await asyncio.sleep(0.05)

        with committed_session_factory() as session:
            r_slow = session.get(models.TaskHistory, slow_id)
            r_next = session.get(models.TaskHistory, followup_id)
            assert r_slow.status == "cancelled"
            assert r_slow.result["summary"] == "任务已手动取消"
            assert r_next.status == "success"
            assert r_next.result["summary"] == "followup-ok"
    finally:
        await mgr.stop()
