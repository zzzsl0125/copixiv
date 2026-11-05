"""Logging sink configuration — rotation, retention, and directory isolation.

These pin the properties that were *missing* before the 2026-10-24 review:

* the sink directory is overridable, so tests stop writing to the production
  ``log/`` (91% of a 344 MB directory was pytest filler);
* rotation is time-based, so the *active* file is rolled over too — loguru only
  hands already-rotated archives to its retention callback, so a size-only
  schedule let ``access.log`` keep growing from 2026-10-08 to 2026-10-24;
* startup prunes stale archives, because ``retention`` only ever runs after a
  rotation and a crash-looping unit may never reach one;
* file sinks run ``diagnose=False`` so tracebacks stop inlining live locals
  (DB URLs, object dumps) at 2.1x the bytes.
"""

import datetime
import os
import time

import pytest

from copixiv.log import (
    LOG_DIR_ENV,
    LOG_RETENTION_DAYS,
    LOG_ROTATION,
    logger,
    setup_logging,
)


@pytest.fixture
def log_dir(tmp_path):
    """Call ``setup_logging`` against a temp dir, restoring sinks afterwards."""
    d = tmp_path / "logs"
    setup_logging(str(d))
    yield d
    logger.remove()
    # Leave the module in its unconfigured state so a later test that calls
    # setup_logging() does not inherit this directory's sinks.
    logger.add(os.devnull)


def _file_handler(directory, name):
    """The loguru handler writing to ``directory/name``."""
    for handler in logger._core.handlers.values():
        sink = getattr(handler, "_sink", None)
        path = getattr(sink, "_path", None)
        if path and path.endswith(name):
            return handler
    raise AssertionError(f"no handler for {name!r} in {directory}")


def _file_sink(directory, name):
    """The loguru file sink writing to ``directory/name``."""
    return _file_handler(directory, name)._sink


def test_env_var_selects_the_log_directory(tmp_path, monkeypatch):
    """``$COPIXIV_LOG_DIR`` keeps tests out of the production log dir."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    setup_logging()
    try:
        assert (tmp_path / "backend.log").exists()
        assert (tmp_path / "access.log").exists()
    finally:
        logger.remove()
        logger.add(os.devnull)


def test_explicit_argument_beats_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "from-env"))
    setup_logging(str(tmp_path / "explicit"))
    try:
        assert (tmp_path / "explicit" / "backend.log").exists()
        assert not (tmp_path / "from-env").exists()
    finally:
        logger.remove()
        logger.add(os.devnull)


def test_rotation_is_time_based_so_the_active_file_rolls(log_dir):
    """A size-only schedule never rotates a quiet, long-lived file."""
    assert LOG_ROTATION == "00:00"
    sink = _file_sink(log_dir, "backend.log")
    logger.info("seed")
    rotation = sink._rotation_function

    class _Message:
        def __init__(self, when):
            self.record = {"time": when}

    now = datetime.datetime.now().astimezone()
    assert rotation(_Message(now), sink._file) is False, "同一天不应轮换"
    assert rotation(_Message(now + datetime.timedelta(days=1)), sink._file) is True


def test_startup_pruning_never_deletes_the_active_log(log_dir):
    """After a long outage the active file's mtime looks like an archive's.

    ``backend.log`` is not an archive, so the sweep must skip it even when it
    is older than the retention window — otherwise a restart would delete the
    file the new process is about to append to.
    """
    active = log_dir / "backend.log"
    active.write_bytes(b"the live file")
    old = time.time() - (LOG_RETENTION_DAYS + 30) * 86400
    os.utime(active, (old, old))

    setup_logging(str(log_dir))
    try:
        assert active.exists(), "活动日志文件不能被启动清理删掉"
        assert active.read_bytes() == b"the live file"
    finally:
        logger.remove()
        logger.add(os.devnull)


def test_startup_prunes_archives_older_than_the_window(log_dir):
    """Retention alone never fires without a rotation; startup must sweep."""
    stale = log_dir / "backend.2020-01-01_00-00-00_000000.log.gz"
    fresh = log_dir / "backend_just_now.log.gz"
    for path in (stale, fresh):
        path.write_bytes(b"x")

    old = time.time() - (LOG_RETENTION_DAYS + 1) * 86400
    os.utime(stale, (old, old))

    # Re-run setup: this is what a service restart does.
    setup_logging(str(log_dir))
    try:
        assert not stale.exists(), "超过保留窗口的归档必须在启动时清掉"
        assert fresh.exists(), "窗口内的归档必须保留"
    finally:
        logger.remove()
        logger.add(os.devnull)


def test_startup_pruning_survives_an_unreadable_entry(log_dir, monkeypatch):
    """Log maintenance must never keep the app from starting."""
    (log_dir / "backend.2020-01-01_00-00-00_000000.log").write_bytes(b"x")

    def _boom(*_args, **_kwargs):
        raise OSError("EPERM")

    monkeypatch.setattr(os, "utime", _boom)
    setup_logging(str(log_dir))  # must not raise
    logger.remove()
    logger.add(os.devnull)


def test_file_sinks_drop_the_variable_dump(log_dir):
    """``diagnose`` inlines live locals — file sinks must not."""
    for name in ("backend.log", "access.log"):
        handler = _file_handler(log_dir, name)
        formatter = handler._exception_formatter
        assert formatter._diagnose is False, f"{name} 仍在 dump 变量值"
        assert formatter._backtrace is True, f"{name} 丢了上游调用链"
