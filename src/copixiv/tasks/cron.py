"""In-memory cron ticker driven by the ``scheduled_task`` table."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from copixiv.log import logger
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .registry import get_spec


def _resolve_cron_timezone() -> tzinfo:
    """Resolve an IANA ZoneInfo timezone when available, else local tzinfo."""
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, ValueError):
            pass

    localtime = Path("/etc/localtime")
    try:
        if localtime.is_symlink():
            target = str(localtime.resolve())
            if "/zoneinfo/" in target:
                zone_name = target.split("/zoneinfo/", 1)[1]
                return ZoneInfo(zone_name)
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass

    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


CRON_TIMEZONE: tzinfo = _resolve_cron_timezone()
CRON_TICK_SECONDS = 30
CRON_MISFIRE_GRACE_SECONDS = 60


def next_occurrence(cron: str, after: datetime) -> datetime:
    """Return the first cron occurrence strictly after *after*."""
    if not isinstance(cron, str) or len(cron.split()) != 5:
        raise ValueError(f"Cron expression must have exactly 5 whitespace-separated fields: {cron!r}")
    if after.tzinfo is None:
        after = after.replace(tzinfo=CRON_TIMEZONE)
    return croniter(cron, after).get_next(datetime)


@dataclass
class _CronEntry:
    """One loaded cron row and its cached next occurrence."""

    scheduled_id: int
    display_name: str
    func_name: str
    params: dict
    cron: str
    next_run: datetime


class CronTicker:
    """In-memory cron ticker driven by ``scheduled_tasks`` rows."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._entries: dict[int, _CronEntry] = {}
        self._enqueue = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reload_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self, enqueue) -> None:
        """Load the current cron rows and start the tick loop."""
        self._stopped = False
        self._loop = asyncio.get_running_loop()
        self._reload_event = asyncio.Event()
        self.load_cron_jobs(enqueue)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the tick loop (cancelling the sleep) and clear the task."""
        self._stopped = True
        self._signal_reload()
        task = self._task
        self._task = None
        self._loop = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("CronTicker stopped.")

    def _signal_reload(self) -> None:
        """Wake the sleep loop immediately when schedules change."""
        event = self._reload_event
        if event is None:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            event.set()
            return
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    def _next_sleep_seconds(self, now: datetime) -> float:
        """Compute bounded seconds until the next due entry (never busy-loops)."""
        with self._lock:
            if not self._entries:
                return float(CRON_TICK_SECONDS)
            earliest = min(entry.next_run for entry in self._entries.values())
        delay = (earliest - now).total_seconds()
        return max(0.0, min(delay, float(CRON_TICK_SECONDS)))

    async def _run(self) -> None:
        """Sleep until next due entry (capped at CRON_TICK_SECONDS), fire due jobs."""
        while not self._stopped:
            if self._reload_event is not None:
                self._reload_event.clear()
            delay = self._next_sleep_seconds(datetime.now(CRON_TIMEZONE))
            if delay > 0 and self._reload_event is not None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._reload_event.wait(),
                        timeout=delay,
                    )
            if self._stopped:
                break
            try:
                self._fire_due(datetime.now(CRON_TIMEZONE))
            except Exception:
                logger.exception("Cron ticker iteration failed.")

    def reload_cron_jobs(self, enqueue, *, now: datetime | None = None) -> None:
        """Reload all cron entries from the database and wake the sleep loop."""
        self.load_cron_jobs(enqueue, now=now)
        self._signal_reload()

    def load_cron_jobs(self, enqueue, *, now: datetime | None = None) -> None:
        """Read ``scheduled_tasks`` and rebuild the in-memory entry map."""
        if now is None:
            now = datetime.now(CRON_TIMEZONE)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=CRON_TIMEZONE)

        with self._session_factory() as session:
            try:
                tasks = SQLAlchemyTaskRepository(session).get_scheduled_tasks()
            except Exception:
                logger.exception("Failed to load scheduled tasks from database.")
                return

        with self._lock:
            entries: dict[int, _CronEntry] = {}
            for task in tasks:
                if not task.is_enabled:
                    continue

                if get_spec(task.task) is None:
                    logger.warning(
                        "Scheduled task '{}' references unknown function '{}' — skipped.",
                        task.name,
                        task.task,
                    )
                    continue

                existing = self._entries.get(task.id)
                if existing is not None and existing.cron == task.cron:
                    next_run = existing.next_run
                else:
                    try:
                        next_run = next_occurrence(task.cron, now)
                    except ValueError:
                        logger.warning(
                            "Scheduled task '{}' (id={}) has invalid cron '{}' — skipped.",
                            task.name,
                            task.id,
                            task.cron,
                        )
                        continue

                entries[task.id] = _CronEntry(
                    scheduled_id=task.id,
                    display_name=task.name,
                    func_name=task.task,
                    params=task.params or {},
                    cron=task.cron,
                    next_run=next_run,
                )
                logger.info(
                    "Registered cron job: {} (id={}, cron='{}', next_run={})",
                    task.name,
                    task.id,
                    task.cron,
                    next_run,
                )

            self._entries = entries
            self._enqueue = enqueue

    def _fire_due(self, now: datetime) -> None:
        """Enqueue every entry due at *now* and advance its next run."""
        to_enqueue: list[tuple[int, str, str, dict]] = []
        with self._lock:
            enqueue = self._enqueue
            for entry in self._entries.values():
                while entry.next_run <= now:
                    late = (now - entry.next_run).total_seconds()
                    if late > CRON_MISFIRE_GRACE_SECONDS:
                        logger.warning(
                            "Cron task '{}' (id={}, cron='{}') missed its "
                            "scheduled run at {} ({}s late) — skipped, next run "
                            "advanced past now.",
                            entry.display_name,
                            entry.scheduled_id,
                            entry.cron,
                            entry.next_run,
                            late,
                        )
                        entry.next_run = next_occurrence(entry.cron, now)
                        break

                    logger.info("Cron triggered: {}", entry.display_name)
                    to_enqueue.append(
                        (
                            entry.scheduled_id,
                            entry.display_name,
                            entry.func_name,
                            dict(entry.params),
                        )
                    )
                    entry.next_run = next_occurrence(entry.cron, entry.next_run)

        if enqueue is None:
            return
        for scheduled_id, display_name, func_name, params in to_enqueue:
            try:
                enqueue(display_name, func_name, params=params)
            except Exception:
                logger.exception(
                    "Failed to enqueue cron task '{}' (id={}).",
                    display_name,
                    scheduled_id,
                )
