"""Instance-exclusive lock (``flock``) tests — R1 / F12.

The composition root acquires an exclusive ``flock`` on ``<db path>.lock``
before building the engine, so a second copixiv process pointed at the same
database fails fast with ``SystemExit`` ("另一个 copixiv 实例正在使用此数据库")
instead of silently corrupting the shared SQLite file.

``flock`` locks are per *open file description*, so a second ``open()`` on
the same path in the **same** process is denied even though it is the same
process — this is precisely what these tests exercise.  No real database is
needed; the lock file is a sibling of the (non-existent) DB path.
"""

import pytest

from copixiv.app import _acquire_instance_lock

try:
    import fcntl  # noqa: F401 — presence check only
except ImportError:  # pragma: no cover — non-POSIX
    fcntl = None

pytestmark = pytest.mark.skipif(
    fcntl is None,
    reason="instance lock uses fcntl (POSIX only)",
)


def test_second_acquire_same_path_is_rejected_then_recovers(tmp_path):
    """First lock succeeds; a second lock on the same path is rejected with
    SystemExit; after closing the first fd the path locks normally again."""
    fake_db = tmp_path / "db" / "app.db"

    # First acquire — the parent directory may not exist yet; the lock
    # helper creates it before opening the lock file.
    fd1 = _acquire_instance_lock(str(fake_db))
    assert fd1 is not None
    assert (tmp_path / "db" / "app.db.lock").exists()

    # Second acquire on the same path is denied (as a second instance).
    with pytest.raises(SystemExit) as exc:
        _acquire_instance_lock(str(fake_db))
    assert str(exc.value) == "另一个 copixiv 实例正在使用此数据库"

    # Closing the first fd releases the lock: a third acquire succeeds.
    fd1.close()
    fd2 = _acquire_instance_lock(str(fake_db))
    assert fd2 is not None
    fd2.close()
