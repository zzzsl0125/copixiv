import inspect
import json
import traceback
import asyncio
import time
import concurrent.futures
from typing import Callable, Any, Tuple, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_MAX_INSTANCES

from core.db.database import db
from core.db.repositories.task import TaskHistory, ScheduledTask as DBScheduledTask
from core.util import import_string
from core.notifier import notifier
from core.logger import capture_logs, logger

async def _execute_func(func: Callable, kwargs: dict) -> Any:
    """
    Executes a synchronous or asynchronous function with a 30-minute timeout.
    Runs asynchronously within the main event loop.
    Synchronous functions are executed in a ThreadPoolExecutor.
    """
    if inspect.iscoroutinefunction(func):
        return await asyncio.wait_for(func(**kwargs), timeout=1800)
    else:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return await asyncio.wait_for(loop.run_in_executor(pool, lambda: func(**kwargs)), timeout=1800)

def _parse_task_result(result_val: Any, config: dict) -> Tuple[List[str], int]:
    """Extracts the number of novels and titles from a task's return value."""
    titles = []
    count = 0
    if isinstance(result_val, list):
        titles = result_val
        count = len(titles)
    elif isinstance(result_val, int):
        count = result_val
        
    if config.get('notify_on_new_novel') is False:
        titles = []
        
    return titles, count

async def run_task_wrapper(task_id: int, name: str, func: Callable, config: dict, kwargs: dict):
    """
    APScheduler job wrapper: Handles state tracking, logging, error handling, and notifications.
    """
    logger.info(f"Starting task '{name}' (ID: {task_id})...")
    start_time = time.time()
    status, error_msg, result_val = "running", None, None

    # Run sync DB op in executor to avoid blocking the loop
    loop = asyncio.get_running_loop()
    def _update_running():
        with db.get_session() as session:
            TaskHistory(session).update_task(task_id, "running")
    await loop.run_in_executor(None, _update_running)

    with capture_logs(task_id=task_id) as get_logs:
        try:
            result_val = await _execute_func(func, kwargs)
            status = "success"
            logger.info(f"Task '{name}' (ID: {task_id}) completed successfully.")
        except (asyncio.TimeoutError, TimeoutError):
            status, error_msg = "success", "Task timed out after 30 minutes"
            logger.error(f"Task '{name}' (ID: {task_id}) finish: {error_msg}")
            logger.error(traceback.format_exc())
        except Exception as e:
            status, error_msg = "failed", str(e)
            logger.error(f"Task '{name}' (ID: {task_id}) failed: {e}")
            logger.error(traceback.format_exc())
        finally:
            captured_log_str = get_logs()

    duration = time.time() - start_time
    titles, count = _parse_task_result(result_val, config)
    
    result_data = {
        "log": captured_log_str,
        "new_novels_count": count,
        "new_novel_titles": titles
    }

    def _update_finish():
        with db.get_session() as session:
            TaskHistory(session).update_task(task_id, status, result=json.dumps(result_data))
    await loop.run_in_executor(None, _update_finish)

    # notifier.send_task_result is sync right now but makes network requests. We should probably run it in executor too
    # but for now let's just run it synchronously as before or in executor
    await loop.run_in_executor(None, lambda: notifier.send_task_result(
        task_name=name,
        status=status,
        duration=duration if status == 'success' else None,
        error=error_msg,
        new_novels_count=count,
        new_novel_titles=titles if titles else None
    ))

class TaskManagerSystem:
    """
    Manages background tasks and scheduled cron jobs using APScheduler.
    Uses AsyncIOScheduler to run everything on the main asyncio event loop.
    """
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
        self.running = False

    def _on_job_error(self, event):
        logger.error(f"APScheduler Job Error: job_id={event.job_id}, exception={getattr(event, 'exception', 'N/A')}")

    def start(self):
        if not self.running:
            self.running = True
            self.scheduler.start()
            self._load_cron_jobs()
            logger.info("TaskManagerSystem (APScheduler) started in main event loop.")

    def stop(self):
        self.running = False
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("TaskManagerSystem stopped.")

    def add_task(self, name: str, func: Callable, config: dict = None, **kwargs):
        """Adds a manual task to run immediately in the background worker queue."""
        kwargs, config = kwargs or {}, config or {}

        with db.get_session() as session:
            task_id = TaskHistory(session).add_task(name, kwargs)
            logger.info(f"Task '{name}' (ID: {task_id}) added to APScheduler queue.")

        self.scheduler.add_job(
            run_task_wrapper,
            args=(task_id, name, func, config, kwargs)
        )

    def reload_cron_jobs(self):
        """Reloads all cron jobs from the database dynamically."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith('cron_'):
                job.remove()
        self._load_cron_jobs()

    def _load_cron_jobs(self):
        """Loads and schedules active jobs from the database."""
        with db.get_session() as session:
            tasks = DBScheduledTask(session).get_all()
            
            for task in tasks:
                if not task.is_enabled:
                    continue
                
                try:
                    task_path = task.task if '.' in task.task else f"core.tasks.{task.task}"
                    func = import_string(task_path)
                    params, config = self._parse_json(task.params), self._parse_json(task.config)

                    self.scheduler.add_job(
                        self._trigger_cron_job,
                        trigger=CronTrigger.from_crontab(task.cron),
                        id=f'cron_{task.id}',
                        args=(task.name, func, config, params),
                        replace_existing=True,
                        max_instances=1,
                        misfire_grace_time=60
                    )
                    logger.debug(f"Registered cron job: {task.name} ('{task.cron}')")
                    
                except Exception as e:
                    logger.error(f"Failed to register scheduled task '{task.name}': {e}")

    def _trigger_cron_job(self, name: str, func: Callable, config: dict, params: dict):
        """Fires when a cron job triggers. Logs it to history and executes."""
        logger.info(f"Cron triggered for task: {name}")
        self.add_task(name, func, config, **params)

    def _parse_json(self, value: Any) -> dict:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

# --- Module-level Singleton ---
# Replace the heavy `__new__` pattern with a standard Python module-level instance.
# This ensures VSCode features robust type-hinting for the instance.
task_manager = TaskManagerSystem()

# Provide direct aliases
task_executor = task_manager
scheduler = task_manager
