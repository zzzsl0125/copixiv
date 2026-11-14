"""Platform logging module — loguru with file rotation + stdlib bridge.

This is a **platform module** (see ``docs/MODULARITY.md`` §M0): every
layer may import it; it belongs to no layer.  Call ``setup_logging()``
once at startup (the entry point does) and then use
``from copixiv.log import logger`` everywhere.

Moved out of the former ``app.logger`` module so that ``infrastructure`` /
``application`` no longer depend on the ``app`` layer.  (The old logging
shim was removed — internal code imports from here directly.)
"""

import io
import logging
import os
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Callable, Generator

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
#: How long rotated log archives are kept.  A *daily* rotation plus this
#: window bounds the directory to ~:data:`LOG_RETENTION_DAYS` files no matter
#: how fast the app logs.  The previous ``rotation="10 MB"`` bound nothing on
#: the time axis: a burst could rotate dozens of 10 MB files within minutes,
#: and because every one of them was younger than the 3-day window, retention
#: deleted none of them (344 MB / 41 files observed on 2026-10-24).
LOG_RETENTION_DAYS = 7
#: Rotate at local midnight.  A time-based rotation also means the *active*
#: file is rolled over — loguru only ever passes already-rotated archives to
#: the retention callback, so a size-only schedule can leave one file growing
#: forever (``access.log`` still held 2026-10-08 lines on 2026-10-24).
LOG_ROTATION = "00:00"
#: Compress archives.  Text logs compress to roughly a tenth, which matters
#: far more than the age window when the app is verbose.
LOG_COMPRESSION = "gz"

#: Environment override for the log directory.  Tests set this to a temp dir
#: so a pytest run never appends to the *production* ``log/backend.log``:
#: ``create_app()`` calls :func:`setup_logging`, and the big-log tests emit
#: tens of thousands of filler lines that previously landed in ``log/`` and
#: rotated the real archives away (91% of the directory was test filler).
LOG_DIR_ENV = "COPIXIV_LOG_DIR"

#: Marker prepended when a stored task log was truncated, so nobody wonders
#: where the head of the run went.
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


def _log_dir(log_dir: str | None) -> Path:
    """Resolve the sink directory: explicit arg → ``COPIXIV_LOG_DIR`` → ``log``.

    The env override is what keeps tests out of the production directory; see
    :data:`LOG_DIR_ENV`.
    """
    return Path(log_dir or os.environ.get(LOG_DIR_ENV) or "log")


def _prune_old_logs(directory: Path, pattern: str, days: int) -> None:
    """Delete archives in *directory* whose mtime is older than *days*.

    Runs once at startup.  loguru's own ``retention`` is only consulted after
    a rotation, so a process that boots, writes a little, and is restarted
    repeatedly (``Restart=always`` on a crash-looping unit) never trims
    anything.  Startup is the one moment that is guaranteed to happen.

    *pattern* must match archives only.  ``backend.log`` is not an archive,
    and after a long outage its mtime looks just as old as one — sweeping it
    would discard the file the new process is about to append to.
    """
    cutoff = date.today().toordinal() - days
    for path in directory.glob(pattern):
        try:
            if not path.is_file():
                continue
            if date.fromtimestamp(path.stat().st_mtime).toordinal() < cutoff:
                path.unlink()
        except OSError:
            # A log we cannot stat or remove must never keep the app from
            # starting — logging is a side channel, not a dependency.
            continue


def setup_logging(log_dir: str | None = None) -> None:
    """Configure loguru sinks (console + rotating files) and intercept stdlib.

    Must be called once, early in the process lifetime — ideally before any
    ``logger`` calls are made.

    *log_dir* defaults to ``$COPIXIV_LOG_DIR``, else the repo-local ``log/``.
    Callers that are not the production service (tests, dev harnesses) should
    pass their own directory so they do not append to — or rotate away — the
    real archives.
    """
    directory = _log_dir(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    # Trim stragglers first: loguru's retention only fires *after* a rotation,
    # so nothing would ever clean up a directory already full of stale files.
    # The patterns skip the active ``backend.log`` / ``access.log`` (see
    # ``_prune_old_logs``); rotated archives are named ``<stem>.<timestamp>.log``
    # optionally followed by ``.gz``.
    for stem in ("backend", "access"):
        _prune_old_logs(directory, f"{stem}.*.log*", LOG_RETENTION_DAYS)

    # Remove default handler
    logger.remove()

    # Shared format
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "<level>{level}</level> "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # Console (stdout, colourised).  Keeps loguru's default
    # ``backtrace=True, diagnose=True``: a human is reading it live and the
    # annotated locals are genuinely useful there.
    logger.add(sys.stdout, format=log_format, level="INFO", colorize=True)

    # File sinks.  ``diagnose=False`` drops the per-frame variable dump
    # (``│ └ <function foo at 0x7f…>``, 2.1x the bytes) while keeping
    # ``backtrace=True`` so the *caller* chain above the raise point survives —
    # that chain is what tells you who triggered the failure.  The dump also
    # inlined live values (DB URLs, tokens) into the log.
    file_kwargs = dict(
        format=log_format,
        level="INFO",
        rotation=LOG_ROTATION,
        retention=f"{LOG_RETENTION_DAYS} days",
        compression=LOG_COMPRESSION,
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
    )

    # Backend log file — all application / library messages.
    # Exclude only HTTP access-log entries produced by the access-log
    # middleware (tagged with ``extra.name == "http_access"``).
    logger.add(
        directory / "backend.log",
        filter=lambda record: record["extra"].get("name") != "http_access",
        **file_kwargs,
    )

    # Access log file — HTTP request/response entries from the access-log
    # middleware (tagged with ``extra.name == "http_access"``).
    logger.add(
        directory / "access.log",
        filter=lambda record: record["extra"].get("name") == "http_access",
        **file_kwargs,
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
