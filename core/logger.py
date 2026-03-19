import sys
import logging
import io
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Generator, Callable

from loguru import logger

# ContextVar to track the current task ID for isolated logging
current_task_id: ContextVar[int] = ContextVar("current_task_id", default=None)

class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages and route them to Loguru.
    """
    def emit(self, record):
        # Get corresponding Loguru level if it exists.
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

def setup_logging(log_dir='log'):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Define standard format
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level}</level> <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"

    # Console handler
    logger.add(sys.stdout, format=log_format, level="INFO", colorize=True)

    # Backend log file handler
    logger.add(
        log_dir / "backend.log",
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 days",
        encoding="utf-8",
        filter=lambda record: record["extra"].get("name") != "frontend" and record["name"] != "uvicorn.access" and record["name"] != "logging"
    )

    # Frontend log file handler
    logger.add(
        log_dir / "frontend.log",
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 days",
        encoding="utf-8",
        filter=lambda record: record["extra"].get("name") == "frontend"
    )

    # Intercept standard logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    
    # Set specific third-party library log levels to avoid noise
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    
    # Configure uvicorn loggers to intercept
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers = [InterceptHandler()]
        uv_logger.propagate = False
        
    # We want access logs to show in access.log
    logger.add(
        log_dir / "access.log",
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 days",
        encoding="utf-8",
        filter=lambda record: record["name"] == "uvicorn.access" or record["name"] == "logging"
    )

setup_logging()

@contextmanager
def capture_logs(task_id: int = None, level="INFO") -> Generator[Callable[[], str], None, None]:
    """
    Capture logs for the current context (e.g., a specific task execution).
    """
    buffer = io.StringIO()
    
    def task_filter(record):
        # If task_id is provided, only capture logs bound to this task_id
        if task_id is not None:
            return record["extra"].get("task_id") == task_id
        return True

    handler_id = logger.add(
        buffer, 
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}", 
        filter=task_filter,
        level=level
    )
    
    try:
        if task_id is not None:
            with logger.contextualize(task_id=task_id):
                yield buffer.getvalue
        else:
            yield buffer.getvalue
    finally:
        logger.remove(handler_id)
        buffer.close()
