"""Platform logging module — loguru with file rotation + stdlib bridge."""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from types import FrameType

    from loguru import Record

MAX_TASK_LOG_BYTES = 256 * 1024
LOG_RETENTION_DAYS = 7
LOG_ROTATION = "00:00"
LOG_COMPRESSION = "gz"
LOG_DIR_ENV = "COPIXIV_LOG_DIR"

_TRUNCATION_NOTICE = "[日志过长，仅保留最后 {} KiB；完整日志见 log/backend.log]\n"


class _BoundedLogRingBuffer:
    """Thread-safe bounded ring buffer keeping the tail up to ``max_bytes``."""

    def __init__(self, max_bytes: int = MAX_TASK_LOG_BYTES) -> None:
        self._max_bytes = max(max_bytes, 1)
        self._lock = threading.Lock()
        self._lines: deque[tuple[str, int]] = deque()
        self._total_bytes: int = 0
        self._truncated: bool = False

    def write(self, message: str) -> int:
        if not message:
            return 0
        raw = message.encode("utf-8", errors="replace")
        msg_bytes = len(raw)
        with self._lock:
            if msg_bytes >= self._max_bytes:
                tail = raw[-self._max_bytes :]
                newline = tail.find(b"\n")
                if 0 <= newline < len(tail) - 1:
                    tail = tail[newline + 1 :]
                tail_text = tail.decode("utf-8", errors="replace")
                tail_bytes = len(tail_text.encode("utf-8"))
                self._lines.clear()
                self._lines.append((tail_text, tail_bytes))
                self._total_bytes = tail_bytes
                self._truncated = True
                return len(message)

            self._lines.append((message, msg_bytes))
            self._total_bytes += msg_bytes
            while self._total_bytes > self._max_bytes and len(self._lines) > 1:
                _, dropped = self._lines.popleft()
                self._total_bytes -= dropped
                self._truncated = True
        return len(message)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        with self._lock:
            body = "".join(text for text, _ in self._lines)
            truncated = self._truncated
        if not truncated:
            return body
        kibs = max(self._max_bytes // 1024, 1)
        return _TRUNCATION_NOTICE.format(kibs) + body


class InterceptHandler(logging.Handler):
    """Intercept standard library log records and route them to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame: FrameType | None = logging.currentframe()
        depth = 2
        if frame is not None:
            frame = frame.f_back
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _log_dir(log_dir: str | None) -> Path:
    return Path(log_dir or os.environ.get(LOG_DIR_ENV) or "log")


def _prune_old_logs(directory: Path, pattern: str, days: int) -> None:
    cutoff = date.today().toordinal() - days
    for path in directory.glob(pattern):
        try:
            if not path.is_file():
                continue
            if date.fromtimestamp(path.stat().st_mtime).toordinal() < cutoff:
                path.unlink()
        except OSError:
            continue


def setup_logging(log_dir: str | None = None) -> None:
    """Configure loguru sinks (console + rotating files) and intercept stdlib."""
    directory = _log_dir(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    for stem in ("backend", "access"):
        _prune_old_logs(directory, f"{stem}.*.log*", LOG_RETENTION_DAYS)

    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "<level>{level}</level> "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # Disable diagnose on stdout as well so live secrets never hit journald.
    logger.add(
        sys.stdout,
        format=log_format,
        level="INFO",
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    file_sinks: list[tuple[str, Callable[[Record], bool]]] = [
        ("backend.log", lambda record: record["extra"].get("name") != "http_access"),
        ("access.log", lambda record: record["extra"].get("name") == "http_access"),
    ]
    for filename, sink_filter in file_sinks:
        logger.add(
            directory / filename,
            format=log_format,
            level="INFO",
            filter=sink_filter,
            rotation=LOG_ROTATION,
            retention=f"{LOG_RETENTION_DAYS} days",
            compression=LOG_COMPRESSION,
            encoding="utf-8",
            backtrace=True,
            diagnose=False,
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Quiet noisy HTTP client loggers routed through InterceptHandler.
    for noisy_logger in ("httpx", "httpcore"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers = [InterceptHandler()]
        uv_logger.propagate = False


@contextmanager
def capture_logs(
    task_id: int | None = None,
    level: str = "INFO",
    *,
    max_bytes: int = MAX_TASK_LOG_BYTES,
) -> Generator[Callable[[], str], None, None]:
    """Capture loguru output in a bounded thread-safe ring buffer."""
    buffer = _BoundedLogRingBuffer(max_bytes=max_bytes)
    _done: list[str] = [""]

    def task_filter(record: Record) -> bool:
        if task_id is not None:
            return record["extra"].get("task_id") == task_id
        return True

    handler_id = logger.add(
        buffer,
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
        filter=task_filter,
        level=level,
        diagnose=False,
    )

    def get_logs() -> str:
        return _done[0] if _done[0] else buffer.getvalue()

    try:
        if task_id is not None:
            with logger.contextualize(task_id=task_id):
                yield get_logs
        else:
            yield get_logs
    finally:
        logger.remove(handler_id)
        _done[0] = buffer.getvalue()
