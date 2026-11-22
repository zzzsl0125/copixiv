"""Layer 1 unit tests: task registry, CronTicker scheduling, and TaskExecutor lifecycle."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from copixiv.core.models import TaskResult
from copixiv.db.backup import backup_database
from copixiv.features.library.ingest import parse_filename
from copixiv.storage.encoding import decode_text_bytes
from copixiv.tasks import registry
from copixiv.tasks.cron import CRON_TIMEZONE, CronTicker, next_occurrence
from copixiv.tasks.executor import TaskContext, TaskExecutor
from copixiv.tasks.schemas import ScheduledTaskResponse


class _SampleArgs(BaseModel):
    count: int = 1


class _RichSampleArgs(BaseModel):
    count: int = 1
    flag: bool = False
    limit: int | None = Field(default=None, description="可选上限")
    ids: list[int]


class _FakeRecorder:
    def __init__(self) -> None:
        self.updates: list[tuple[int, str | None, dict | str | None]] = []

    async def update(
        self,
        task_id: int,
        status: str | None,
        result: str | None = None,
        duration: float | None = None,
        progress: str | None = None,
    ) -> None:
        del duration, progress
        parsed = json.loads(result) if isinstance(result, str) else result
        self.updates.append((task_id, status, parsed))


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        result: TaskResult | None = None,
    ) -> None:
        self.calls.append(
            {
                "task_name": task_name,
                "status": status,
                "duration": duration,
                "error": error,
                "result": result,
            }
        )


def test_registry_register_rejects_sync_and_exposes_schema(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(TypeError, match="async def"):

        @registry.register("bad_sync_task")
        def _sync_fn():
            return None

    @registry.register("unit_sample_task", description="Sample task", args=_RichSampleArgs)
    async def _sample_fn(args: _RichSampleArgs, ctx: TaskContext):
        del ctx
        return TaskResult(summary=f"ok:{args.count}")

    monkeypatch.setitem(registry._registry, "unit_sample_task", registry._registry["unit_sample_task"])
    spec = registry.get_spec("unit_sample_task")
    assert spec is not None
    assert spec.description == "Sample task"
    assert registry.get_task("unit_sample_task") is _sample_fn

    described = {m["name"]: m for m in registry.describe_tasks()}
    assert "unit_sample_task" in described
    args_by_name = {a["name"]: a for a in described["unit_sample_task"]["arguments"]}
    assert args_by_name["count"] == {
        "name": "count",
        "type": "integer",
        "required": False,
        "nullable": False,
        "default": 1,
        "description": None,
    }
    assert args_by_name["flag"]["type"] == "boolean"
    assert args_by_name["limit"] == {
        "name": "limit",
        "type": "integer",
        "required": False,
        "nullable": True,
        "default": None,
        "description": "可选上限",
    }
    assert args_by_name["ids"] == {
        "name": "ids",
        "type": "array",
        "required": True,
        "nullable": False,
        "default": None,
        "description": None,
    }


def test_cron_next_occurrence_and_fire_due(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="5 whitespace-separated"):
        next_occurrence("* * *", datetime.now(UTC))

    base = datetime(2026, 6, 1, 10, 0, tzinfo=CRON_TIMEZONE)
    nxt = next_occurrence("15 10 * * *", base)
    assert nxt == datetime(2026, 6, 1, 10, 15, tzinfo=CRON_TIMEZONE)

    # Mock session factory returning enabled, disabled, unknown, and invalid cron rows
    rows = [
        SimpleNamespace(
            id=1, name="due", task="unit_cron_task", cron="0 * * * *", params={"x": 1}, is_enabled=True
        ),
        SimpleNamespace(
            id=2, name="late", task="unit_cron_task", cron="0 * * * *", params={}, is_enabled=True
        ),
        SimpleNamespace(
            id=3, name="off", task="unit_cron_task", cron="0 * * * *", params={}, is_enabled=False
        ),
        SimpleNamespace(
            id=4, name="missing_fn", task="no_such_task", cron="0 * * * *", params={}, is_enabled=True
        ),
        SimpleNamespace(id=5, name="bad_cron", task="unit_cron_task", cron="bad", params={}, is_enabled=True),
    ]

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _FakeRepo:
        def __init__(self, _session):
            pass

        def get_scheduled_tasks(self):
            return rows

    @registry.register("unit_cron_task")
    async def _cron_fn(ctx: TaskContext):
        del ctx

    monkeypatch.setitem(registry._registry, "unit_cron_task", registry._registry["unit_cron_task"])
    monkeypatch.setattr("copixiv.tasks.cron.SQLAlchemyTaskRepository", _FakeRepo)

    fired: list[tuple[str, str, dict | None]] = []

    def _enqueue(display_name: str, func_name: str, params: dict | None = None) -> None:
        fired.append((display_name, func_name, params))

    ticker = CronTicker(_FakeSession)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=CRON_TIMEZONE)
    ticker.load_cron_jobs(_enqueue, now=now - timedelta(minutes=5))
    assert set(ticker._entries) == {1, 2}

    # Unchanged cron preserves next_run on reload
    prev_next_1 = ticker._entries[1].next_run
    ticker.load_cron_jobs(_enqueue, now=now)
    assert ticker._entries[1].next_run == prev_next_1

    # Make entry 2 misfire beyond grace window (CRON_MISFIRE_GRACE_SECONDS = 60)
    ticker._entries[2].next_run = now - timedelta(minutes=5)
    ticker._fire_due(now)
    assert fired == [("due", "unit_cron_task", {"x": 1})]
    assert ticker._entries[1].next_run > now
    assert ticker._entries[2].next_run > now


def test_task_context_guards():
    ctx = TaskContext()
    with pytest.raises(RuntimeError, match="session_factory"):
        ctx.child_uow()
    with pytest.raises(RuntimeError, match="uow"):
        ctx.require_uow()
    with pytest.raises(RuntimeError, match="client"):
        ctx.require_client()
    with pytest.raises(RuntimeError, match="file_storage"):
        ctx.require_file_storage()

    sentinel_uow = object()
    child = ctx.with_uow(sentinel_uow)  # type: ignore[arg-type]
    assert child.require_uow() is sentinel_uow
    assert ctx.uow is None


@pytest.mark.asyncio
async def test_task_executor_success_failure_timeout_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
):
    executor = TaskExecutor(session_factory=lambda: None, deps={})

    async def _ok_task(args: _SampleArgs, ctx: TaskContext):
        del ctx
        return TaskResult(summary=f"done:{args.count}", new_novel_titles=["N1"])

    async def _fail_task(ctx: TaskContext):
        del ctx
        raise RuntimeError("boom!")

    async def _slow_task(ctx: TaskContext):
        del ctx
        await asyncio.sleep(5)

    recorder = _FakeRecorder()
    notifier = _FakeNotifier()

    # 1. Success
    spec_ok = registry.TaskSpec(name="ok", description="", func=_ok_task, args_model=_SampleArgs)
    await executor.run_and_record(spec_ok, {"count": 3}, 10, recorder=recorder, notifier=notifier)  # type: ignore[arg-type]
    assert [s for _, s, _ in recorder.updates] == ["running", "success"]
    final_res = recorder.updates[-1][2]
    assert isinstance(final_res, dict)
    assert final_res["summary"] == "done:3"
    assert final_res["new_novels_count"] == 1
    assert notifier.calls[-1]["status"] == "success"

    # 2. Exception -> failed with error message summary
    recorder.updates.clear()
    spec_fail = registry.TaskSpec(name="fail", description="", func=_fail_task)
    await executor.run_and_record(spec_fail, {}, 11, recorder=recorder, notifier=notifier)  # type: ignore[arg-type]
    assert [s for _, s, _ in recorder.updates] == ["running", "failed"]
    assert recorder.updates[-1][2]["summary"] == "boom!"  # type: ignore[index]
    assert notifier.calls[-1]["status"] == "failed"

    # 3. Timeout -> failed with timeout error
    recorder.updates.clear()
    monkeypatch.setattr(executor, "TIMEOUT_SECONDS", 0.01)
    spec_slow = registry.TaskSpec(name="slow", description="", func=_slow_task)
    await executor.run_and_record(spec_slow, {}, 12, recorder=recorder, notifier=notifier)  # type: ignore[arg-type]
    assert [s for _, s, _ in recorder.updates] == ["running", "failed"]
    assert "timed out" in notifier.calls[-1]["error"]

    # 4. Cancellation -> recorded as interrupted and re-raised
    recorder.updates.clear()
    started = asyncio.Event()

    async def _cancellable_task(ctx: TaskContext):
        del ctx
        started.set()
        await asyncio.sleep(5)

    spec_cancel = registry.TaskSpec(name="cancel", description="", func=_cancellable_task)
    task = asyncio.create_task(
        executor.run_and_record(spec_cancel, {}, 13, recorder=recorder, notifier=notifier)  # type: ignore[arg-type]
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [s for _, s, _ in recorder.updates] == ["running", "interrupted"]


@pytest.mark.asyncio
async def test_phase1_task_context_cron_locking_backup_decode_and_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    # 1. ScheduledTaskResponse tolerates legacy invalid cron rows
    legacy_row = ScheduledTaskResponse.model_validate(
        {
            "id": 1,
            "name": "legacy",
            "task": "check_epub",
            "cron": "not a valid cron",
            "params": {"a": 1},
            "is_enabled": False,
            "sort_index": 0,
        }
    )
    assert legacy_row.params == {"a": 1}

    # 2. TaskContext.report_progress uses child_uow(), not ctx.uow
    used_sessions: list[object] = []

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    class _FakeRepo:
        def __init__(self, session):
            used_sessions.append(session)

        def update_task(self, *_args, **_kwargs):
            return None

    primary_session = object()
    child_session = _FakeSession()
    monkeypatch.setattr("copixiv.tasks.executor.SQLAlchemyTaskRepository", _FakeRepo)
    ctx = TaskContext(
        uow=SimpleNamespace(session=primary_session),  # type: ignore[arg-type]
        session_factory=lambda: child_session,
        task_id=42,
    )
    await ctx.report_progress("50%")
    assert used_sessions == [child_session]

    # 3. CronTicker concurrent reload + _fire_due does not raise RuntimeError
    class _FakeCronSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _FakeCronRepo:
        def __init__(self, _s):
            pass

        def get_scheduled_tasks(self):
            return [
                SimpleNamespace(
                    id=i,
                    name=f"t{i}",
                    task="unit_cron_race",
                    cron="* * * * *",
                    params={},
                    is_enabled=True,
                )
                for i in range(10)
            ]

    @registry.register("unit_cron_race")
    async def _cron_race(ctx: TaskContext):
        del ctx

    monkeypatch.setitem(registry._registry, "unit_cron_race", registry._registry["unit_cron_race"])
    monkeypatch.setattr("copixiv.tasks.cron.SQLAlchemyTaskRepository", _FakeCronRepo)

    ticker = CronTicker(_FakeCronSession)
    errors: list[Exception] = []
    stop = threading.Event()

    def _reloader():
        try:
            for _ in range(30):
                ticker.load_cron_jobs(lambda *_a, **_kw: None)
        except Exception as exc:
            errors.append(exc)
        finally:
            stop.set()

    t = threading.Thread(target=_reloader)
    t.start()
    while not stop.is_set():
        ticker._fire_due(datetime.now(CRON_TIMEZONE))
    t.join()
    assert errors == []

    # 4. backup_database masks password on failure/timeout and cleans up random temp files
    secret_url = "postgresql+psycopg2://u:SUPER_SECRET_PW@127.0.0.1:5433/db"

    def _raise_called(*_a, **_kw):
        raise subprocess.CalledProcessError(1, ["pg_dump"], stderr="connection refused")

    monkeypatch.setattr("copixiv.db.backup.subprocess.run", _raise_called)
    with pytest.raises(RuntimeError) as exc_info:
        backup_database(secret_url, backup_dir=str(tmp_path))
    assert "SUPER_SECRET_PW" not in str(exc_info.value)
    assert "connection refused" in str(exc_info.value)
    assert list(tmp_path.glob("*.tmp")) == []

    def _raise_timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(["pg_dump"], timeout=5)

    monkeypatch.setattr("copixiv.db.backup.subprocess.run", _raise_timeout)
    with pytest.raises(RuntimeError, match="timed out") as timeout_info:
        backup_database(secret_url, backup_dir=str(tmp_path), timeout=5)
    assert "SUPER_SECRET_PW" not in str(timeout_info.value)

    # 4b. backup helpers: _connection_args query port, _pg_bin priority, cleanup_old_backups guard
    from copixiv.db.backup import _connection_args, _pg_bin, cleanup_old_backups

    args, _env = _connection_args("postgresql+psycopg2://postgres@/copixiv?host=/tmp/sock&port=5433")
    assert "--host" in args
    assert args[args.index("--host") + 1] == "/tmp/sock"
    assert "--port" in args
    assert args[args.index("--port") + 1] == "5433"

    fake_pgserver_pkg = tmp_path / "fake_pgserver"
    fake_pg_dump = fake_pgserver_pkg / "pginstall" / "bin" / "pg_dump"
    fake_pg_dump.parent.mkdir(parents=True)
    fake_pg_dump.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "copixiv.db.backup.importlib.util.find_spec",
        lambda name: (
            SimpleNamespace(origin=str(fake_pgserver_pkg / "__init__.py")) if name == "pgserver" else None
        ),
    )
    monkeypatch.setattr("copixiv.db.backup.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("COPIXIV_USE_SYSTEM_PG", raising=False)
    assert _pg_bin("pg_dump") == str(fake_pg_dump.resolve())
    monkeypatch.setenv("COPIXIV_USE_SYSTEM_PG", "1")
    assert _pg_bin("pg_dump") == "/usr/bin/pg_dump"
    monkeypatch.delenv("COPIXIV_USE_SYSTEM_PG", raising=False)

    for bad_keep in (0, -1):
        with pytest.raises(ValueError, match="keep_count"):
            cleanup_old_backups(keep_count=bad_keep, backup_dir=str(tmp_path))

    (tmp_path / "2026-W01.dump").write_bytes(b"1")
    (tmp_path / "2026-W02.dump").write_bytes(b"2")
    (tmp_path / "2026-W03.dump").write_bytes(b"3")
    removed = cleanup_old_backups(keep_count=2, backup_dir=str(tmp_path))
    assert removed == [str(tmp_path / "2026-W01.dump")]
    assert sorted(p.name for p in tmp_path.glob("*.dump")) == ["2026-W02.dump", "2026-W03.dump"]

    # 5. Short Big5 text is not silently misdecoded as GB18030
    big5_text = "這是繁體中文測試"
    assert decode_text_bytes(big5_text.encode("big5"), strict=True) == big5_text

    # 6. Bare extension filename gets non-empty fallback title
    assert parse_filename(".txt") == ("未命名", "未知作者")


@pytest.mark.asyncio
async def test_register_lane_timeout_and_notify_false(monkeypatch: pytest.MonkeyPatch):
    @registry.register("_phase4a_fetch_probe", lane="fetch", timeout=0.02, notify=False)
    async def _probe_task(ctx: TaskContext):
        del ctx
        await asyncio.sleep(5)

    monkeypatch.setitem(
        registry._registry,
        "_phase4a_fetch_probe",
        registry._registry["_phase4a_fetch_probe"],
    )
    assert "_phase4a_fetch_probe" in registry.task_names_for_lane("fetch")
    assert "_phase4a_fetch_probe" not in registry.task_names_for_lane("general")

    spec = registry.get_spec("_phase4a_fetch_probe")
    assert spec is not None
    assert spec.lane == "fetch"
    assert spec.timeout == 0.02
    assert spec.notify is False

    recorder = _FakeRecorder()
    notifier = _FakeNotifier()
    executor = TaskExecutor(session_factory=lambda: None, deps={})
    await executor.run_and_record(spec, {}, 99, recorder=recorder, notifier=notifier)  # type: ignore[arg-type]
    assert [s for _, s, _ in recorder.updates] == ["running", "failed"]
    assert notifier.calls == []
