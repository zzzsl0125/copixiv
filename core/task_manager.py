import queue
import threading
import time
import logging
import inspect
import json
import traceback
import asyncio
from datetime import datetime
from typing import Callable, Any, Dict, Optional
from croniter import croniter

from core.db.database import db
from core.db.repositories.task import TaskHistory, ScheduledTask
from core.util import import_string
from core.notifier import notifier
from core.logger import capture_logs, logger

class TaskExecutor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskExecutor, cls).__new__(cls)
            cls._instance.queue = queue.Queue()
            cls._instance.running = False
            cls._instance.worker_thread = None
        return cls._instance

    def start(self):
        if not self.running:
            self.running = True
            self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="TaskExecutorWorker")
            self.worker_thread.start()
            logger.info("TaskExecutor started.")

    def stop(self):
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
            logger.info("TaskExecutor stopped.")

    def add_task(self, name: str, func: Callable, config: dict, **kwargs):
        """
        Add a task to the queue.
        """
        if kwargs is None:
            kwargs = {}
        if config is None:
            config = {}

        # 1. Record in DB as pending
        with db.get_session() as session:
            repo = TaskHistory(session)
            task_id = repo.add_task(name, kwargs)
            logger.info(f"Task '{name}' (ID: {task_id}) added to queue.")

        # 2. Put in queue
        self.queue.put({
            'id': task_id,
            'name': name,
            'func': func,
            'kwargs': kwargs,
            'config': config
        })

    def _worker(self):
        # Create a single event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            while self.running:
                try:
                    # Wait for a task, timeout to allow checking self.running
                    try:
                        task_item = self.queue.get(timeout=1.0)
                    except queue.Empty:
                        continue

                    task_id = task_item['id']
                    name = task_item['name']
                    func = task_item['func']
                    kwargs = task_item['kwargs']
                    config = task_item['config']

                    # --- Start Task Execution ---
                    logger.info(f"Starting task '{name}' (ID: {task_id})...")
                    
                    start_time = time.time()
                    status = "running"
                    error_msg = None
                    task_result_value = None

                    # Update DB status to running
                    with db.get_session() as session:
                        repo = TaskHistory(session)
                        repo.update_task(task_id, "running")

                    with capture_logs() as get_logs:
                        try:
                            # Execute the function with a 30-minute timeout
                            if inspect.iscoroutinefunction(func):
                                task_result_value = loop.run_until_complete(asyncio.wait_for(func(**kwargs), timeout=1800))
                            else:
                                import concurrent.futures
                                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                    future = pool.submit(func, **kwargs)
                                    task_result_value = future.result(timeout=1800)
                            
                            status = "success"
                            logger.info(f"Task '{name}' (ID: {task_id}) completed successfully.")
                        
                        except (asyncio.TimeoutError, TimeoutError):
                            status = "success"
                            error_msg = "Task timed out after 30 minutes"
                            logger.error(f"Task '{name}' (ID: {task_id}) finish: {error_msg}")
                            logger.error(traceback.format_exc())
                        
                        except Exception as e:
                            status = "failed"
                            error_msg = str(e)
                            logger.error(f"Task '{name}' (ID: {task_id}) failed: {e}")
                            logger.error(traceback.format_exc()) # Log traceback to capture it
                        
                        finally:
                            duration = time.time() - start_time
                            captured_logs = get_logs()

                            new_novels_count = 0
                            new_novel_titles = None

                            if isinstance(task_result_value, list):
                                new_novel_titles = task_result_value
                                new_novels_count = len(new_novel_titles)
                            elif isinstance(task_result_value, int):
                                new_novels_count = task_result_value
                            
                            # By default, send titles if they exist, unless explicitly disabled.
                            if config.get('notify_on_new_novel') is False:
                                new_novel_titles = None

                            # Update DB with result and logs
                            result_data = {
                                "log": captured_logs,
                                "new_novels_count": new_novels_count,
                                "new_novel_titles": new_novel_titles if new_novel_titles else []
                            }

                            with db.get_session() as session:
                                repo = TaskHistory(session)
                                repo.update_task(task_id, status, result=json.dumps(result_data))

                            # Send Notification
                            notifier.send_task_result(
                                task_name=name,
                                status=status,
                                duration=duration if status == 'success' else None,
                                error=error_msg,
                                new_novels_count=new_novels_count,
                                new_novel_titles=new_novel_titles
                            )

                            self.queue.task_done()

                except Exception as e:
                    logger.critical(f"Critical error in TaskExecutor worker: {e}")
                    time.sleep(1) # Prevent tight loop on crash

        finally:
            try:
                # Cancel all pending tasks to gracefully shut down the event loop
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception as e:
                logger.error(f"Error closing event loop: {e}")
            finally:
                loop.close()

class Scheduler:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Scheduler, cls).__new__(cls)
            cls._instance.running = False
            cls._instance.scheduler_thread = None
        return cls._instance

    def start(self):
        if not self.running:
            self.running = True
            self.scheduler_thread = threading.Thread(target=self._loop, daemon=True, name="SchedulerWorker")
            self.scheduler_thread.start()
            logger.info("Scheduler started.")

    def stop(self):
        self.running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
            logger.info("Scheduler stopped.")

    def _loop(self):
        """
        Periodically check for scheduled tasks.
        """
        while self.running:
            try:
                self._check_tasks()
            except Exception as e:
                logger.error(f"Error in Scheduler loop: {e}")
            
            # Calculate time to sleep until the next minute starts
            now = datetime.now()
            sleep_seconds = 60 - now.second
            # Add a small buffer to ensure we are into the next minute
            sleep_seconds += 0.5
            
            # Sleep in small increments to allow stopping
            for _ in range(int(sleep_seconds)):
                if not self.running:
                    break
                time.sleep(1)
            
            if self.running and sleep_seconds % 1 > 0:
                 time.sleep(sleep_seconds % 1)

    def _check_tasks(self):
        with db.get_session() as session:
            repo = ScheduledTask(session)
            tasks = repo.get_all()
            
            now = datetime.now()
            
            for task in tasks:
                if not task.is_enabled:
                    continue
                
                try:
                    # Check if it's time to run
                    # We need to know the last run time. 
                    # Assuming we check if 'now' matches the cron pattern.
                    # A better way with croniter is:
                    # if croniter.match(task.cron, now):
                    
                    # Since we check every minute, we can check if the current minute matches.
                    if croniter.match(task.cron, now):
                        logger.info(f"Triggering scheduled task: {task.name}")
                        
                        # Load function
                        try:
                            task_path = task.task
                            if '.' not in task_path:
                                task_path = f"core.tasks.{task_path}"
                            func = import_string(task_path)
                        except ImportError as e:
                            logger.error(f"Failed to import task function '{task.task}': {e}")
                            continue
                        
                        # Parse params
                        params = {}
                        if task.params:
                            if isinstance(task.params, str):
                                try:
                                    params = json.loads(task.params)
                                except json.JSONDecodeError:
                                    pass
                            elif isinstance(task.params, dict):
                                params = task.params
                        
                        # Parse config
                        config = {}
                        if task.config and isinstance(task.config, str):
                            try:
                                config = json.loads(task.config)
                            except json.JSONDecodeError:
                                pass
                        elif isinstance(task.config, dict):
                            config = task.config

                        # Add to executor
                        TaskExecutor().add_task(task.name, func, config, **params)
                        
                except Exception as e:
                    logger.error(f"Error checking task '{task.name}': {e}")

# Global instances
task_executor = TaskExecutor()
scheduler = Scheduler()
