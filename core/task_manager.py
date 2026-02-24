import queue
import threading
import time
import logging
import io
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
from core.logger import TaskLogHandler

logger = logging.getLogger(__name__)

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

    def add_task(self, name: str, func: Callable, **kwargs):
        """
        Add a task to the queue.
        """
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
            'kwargs': kwargs
        })

    def _worker(self):
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

                # --- Start Task Execution ---
                logger.info(f"Starting task '{name}' (ID: {task_id})...")
                
                # Setup log capture
                log_handler = TaskLogHandler()
                log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
                
                # Attach handler to root logger to capture everything
                root_logger = logging.getLogger()
                root_logger.addHandler(log_handler)
                
                start_time = time.time()
                status = "running"
                error_msg = None

                # Update DB status to running
                with db.get_session() as session:
                    repo = TaskHistory(session)
                    repo.update_task(task_id, "running")

                try:
                    # Execute the function
                    if asyncio.iscoroutinefunction(func):
                        # Use a new loop or run in thread logic? 
                        # Since we are in a thread, we can run asyncio.run()
                        asyncio.run(func(**kwargs))
                    else:
                        func(**kwargs)
                    
                    status = "success"
                    logger.info(f"Task '{name}' (ID: {task_id}) completed successfully.")
                
                except Exception as e:
                    status = "failed"
                    error_msg = str(e)
                    logger.error(f"Task '{name}' (ID: {task_id}) failed: {e}")
                    logger.error(traceback.format_exc()) # Log traceback to capture it
                
                finally:
                    duration = time.time() - start_time
                    
                    # Detach log handler
                    root_logger.removeHandler(log_handler)
                    captured_logs = log_handler.get_logs()
                    log_handler.close()

                    # Update DB with result and logs
                    with db.get_session() as session:
                        repo = TaskHistory(session)
                        repo.update_task(task_id, status, result=captured_logs)

                    # Send Notification
                    notifier.send_task_result(
                        task_name=name,
                        status=status,
                        duration=duration if status == 'success' else None,
                        error=error_msg
                    )

                    self.queue.task_done()

            except Exception as e:
                logger.critical(f"Critical error in TaskExecutor worker: {e}")
                time.sleep(1) # Prevent tight loop on crash

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
            
            # Check every minute (adjust precision as needed)
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1)

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
                            func = import_string(task.task)
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
                        
                        # Add to executor
                        TaskExecutor().add_task(task.name, func, **params)
                        
                except Exception as e:
                    logger.error(f"Error checking task '{task.name}': {e}")

# Global instances
task_executor = TaskExecutor()
scheduler = Scheduler()
