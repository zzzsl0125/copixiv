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


#: How much task log one ``task_history`` row may store.
#:
#: A task's log is written into a JSONB column, and nothing bounded it: the
#: capture buffer grew until the task returned, so one ``fix_series_index`` run
#: stored 1,263,316 characters in a single row.  Across 1,566 rows that made
#: ``result`` total 13 MB, and it is re-read by anyone opening the log or by a
#: future un-stripped list query.
#:
#: 256 KiB keeps the useful part — a normal run's log (median 5.7 kB) is
#: unaffected — while making the pathological case cost a predictable amount.
#: The *tail* is kept: when a task fails, the error and the last actions before
#: it are what the operator needs, and the beginning is usually setup noise.
MAX_TASK_LOG_BYTES = 256 * 1024
#: Marker prepended when a log was truncated, so nobody wonders where the head
#: of the run went.
_TRUNCATION_NOTICE = "[日志过长，仅保留最后 {} KiB；完整日志见 log/backend.log]\n"


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
    # Filled once at block exit; lets ``get_logs`` stay answerable afterwards.
    _done: list[str] = [""]

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

    def get_logs() -> str:
        """The captured text, capped at :data:`MAX_TASK_LOG_BYTES`.

        Returns the **tail** plus a notice, because the end of a run holds the
        error and the last actions — the part an operator actually reads.  A
        log at or under the cap is returned verbatim.

        Readable after the ``with`` block exits as well: the kernel calls it
        *inside* the block (before the final history write), but a caller that
        keeps the closure and asks later must not get "I/O operation on closed
        file" — the text has already been produced by then.
        """
        try:
            text = buffer.getvalue()
        except ValueError:
            # Closed after the block: the snapshot taken at exit is the whole
            # record — no further lines can have arrived once the handler was
            # removed.
            text = _done[0]
        raw = text.encode("utf-8", errors="replace")
        if len(raw) <= MAX_TASK_LOG_BYTES:
            return text
        tail = raw[-MAX_TASK_LOG_BYTES:]
        # Cut on a line boundary where possible so the first retained line is
        # not a half-line, and drop a partial UTF-8 sequence at the split.
        newline = tail.find(b"\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1:]
        kibs = MAX_TASK_LOG_BYTES // 1024
        return (
            _TRUNCATION_NOTICE.format(kibs)
            + tail.decode("utf-8", errors="replace")
        )

    try:
        if task_id is not None:
            with logger.contextualize(task_id=task_id):
                yield get_logs
        else:
            yield get_logs
    finally:
        logger.remove(handler_id)
        # Snapshot before closing so a caller holding the closure can still ask
        # after the block (``_done`` is what ``get_logs`` falls back to).
        try:
            _done[0] = buffer.getvalue()
        except ValueError:
            pass
        buffer.close()
