"""In-memory cron ticker driven by the ``scheduled_task`` table."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from croniter import croniter

from copixiv.log import logger
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .registry import get_spec

CRON_TIMEZONE = datetime.now().astimezone().tzinfo
CRON_TICK_SECONDS = 30
CRON_MISFIRE_GRACE_SECONDS = 60


def next_occurrence(cron: str, after: datetime) -> datetime:
    """Return the first cron occurrence strictly after *after*."""
    if not isinstance(cron, str) or len(cron.split()) != 5:
        raise ValueError(
            "Cron expression must have exactly 5 whitespace-separated "
            f"fields: {cron!r}"
        )
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
        self._entries: dict[int, _CronEntry] = {}
        self._enqueue = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self, enqueue) -> None:
        """Load the current cron rows and start the tick loop."""
        self._stopped = False
        self.load_cron_jobs(enqueue)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the tick loop (cancelling the sleep) and clear the task."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("CronTicker stopped.")

    async def _run(self) -> None:
        """Sleep for one tick, fire due entries, repeat until stopped."""
        while not self._stopped:
            await asyncio.sleep(CRON_TICK_SECONDS)
            try:
                self._fire_due(datetime.now(CRON_TIMEZONE))
            except Exception:
                logger.exception("Cron ticker iteration failed.")

    def reload_cron_jobs(self, enqueue, *, now: datetime | None = None) -> None:
        """Reload all cron entries from the database."""
        self.load_cron_jobs(enqueue, now=now)

    def load_cron_jobs(
        self, enqueue, *, now: datetime | None = None
    ) -> None:
        """Read ``scheduled_tasks`` and rebuild the in-memory entry map."""
        if now is None:
            now = datetime.now(CRON_TIMEZONE)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=CRON_TIMEZONE)

        with self._session_factory() as session:
            try:
                tasks = SQLAlchemyTaskRepository(
                    session
                ).get_scheduled_tasks()
            except Exception:
                logger.exception(
                    "Failed to load scheduled tasks from database."
                )
                return

        entries: dict[int, _CronEntry] = {}
        for task in tasks:
            if not task.is_enabled:
                continue

            if get_spec(task.task) is None:
                logger.warning(
                    "Scheduled task '{}' references unknown function "
                    "'{}' — skipped.",
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
                        "Scheduled task '{}' (id={}) has invalid cron "
                        "'{}' — skipped.",
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
        for entry in list(self._entries.values()):
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
                try:
                    self._enqueue(
                        entry.display_name,
                        entry.func_name,
                        params=entry.params,
                    )
                except Exception:
                    logger.exception(
                        "Failed to enqueue cron task '{}' (id={}).",
                        entry.display_name,
                        entry.scheduled_id,
                    )
                entry.next_run = next_occurrence(entry.cron, entry.next_run)
