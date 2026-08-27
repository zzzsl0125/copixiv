"""Platform logging module — loguru with file rotation + stdlib bridge.

This is a **platform module** (see ``docs/MODULARITY.md`` §M0): every
layer may import it; it belongs to no layer.  Call ``setup_logging()``
once at startup (the entry point does) and then use
``from copixiv.log import logger`` everywhere.

Moved out of the former ``app.logger`` module so that ``infrastructure`` /
``application`` no longer depend on the ``app`` layer.  (The old logging
shim was removed — internal code imports from here directly.)
"""

import sys
import logging
import io
from pathlib import Path
from contextlib import contextmanager
from typing import Generator, Callable

from loguru import logger


class InterceptHandler(logging.Handler):
    """Intercept standard library log records and route them to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where the logged message originated.
        # ``logging.currentframe()`` returns *this* frame (emit), which is
        # NOT in ``logging.__file__``, so we advance past it first, then
        # skip every stdlib-logging frame so the reported location is the
        # real call site (e.g. uvicorn, alembic, sqlalchemy).
        frame, depth = logging.currentframe(), 2
        frame = frame.f_back  # step past emit into the stdlib logging frames
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(log_dir: str = "log") -> None:
    """Configure loguru sinks (console + rotating files) and intercept stdlib.

    Must be called once, early in the process lifetime — ideally before any
    ``logger`` calls are made.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Shared format
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "<level>{level}</level> "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # Console (stdout, colourised)
    logger.add(sys.stdout, format=log_format, level="INFO", colorize=True)

    # Backend log file — all application / library messages.
    # Exclude only HTTP access-log entries produced by the access-log
    # middleware (tagged with ``extra.name == "http_access"``).
    logger.add(
        log_dir / "backend.log",
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 days",
        encoding="utf-8",
        filter=lambda record: record["extra"].get("name") != "http_access",
    )

    # Access log file — HTTP request/response entries from the access-log
    # middleware (tagged with ``extra.name == "http_access"``).
    logger.add(
        log_dir / "access.log",
        format=log_format,
        level="INFO",
        rotation="10 MB",
        retention="3 days",
        encoding="utf-8",
        filter=lambda record: record["extra"].get("name") == "http_access",
    )

    # --- Bridge standard library logging into loguru ---
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # Replace uvicorn's handlers so those logs also go through loguru
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers = [InterceptHandler()]
        uv_logger.propagate = False


@contextmanager
def capture_logs(
    task_id: int | None = None, level: str = "INFO"
) -> Generator[Callable[[], str], None, None]:
    """Capture loguru output in a string buffer, optionally filtered by *task_id*.

    Usage::

        with capture_logs(task_id=42) as get_logs:
            logger.info("doing work")
        print(get_logs())   # → "2026-06-30 12:00:00 - INFO - doing work"
    """
    buffer = io.StringIO()

    def task_filter(record: dict) -> bool:
        if task_id is not None:
            return record["extra"].get("task_id") == task_id
        return True

    handler_id = logger.add(
        buffer,
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
        filter=task_filter,
        level=level,
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
