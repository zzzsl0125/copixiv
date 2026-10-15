#!/usr/bin/env python3
"""Local PostgreSQL dev-instance manager for copixiv (postgres-migration).

Wraps the ``pgserver``-bundled PostgreSQL 16 binaries.  The data directory and
Unix socket live under ``.spike/`` so the sandbox can reach them.

Usage::

    python scripts/pg_dev.py start               # boot the dev instance
    python scripts/pg_dev.py status              # is it running?
    python scripts/pg_dev.py createdb <name>     # create a database
    python scripts/pg_dev.py stop                # stop the instance (keeps data)

Port: 5433 (matches ``AppConfig.database_url`` default).  Socket dir:
``<repo>/.spike/sock``.  Data dir: ``<repo>/.spike/pgdata``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pg_bin_dir() -> Path:
    """Return pgserver's bundled ``bin`` directory.

    Prefers the ``pgserver`` package installed in the *running* interpreter
    (CI installs ``pip install ".[dev]"`` into the runner interpreter, where
    no repo-local ``.venv`` exists); falls back to the repo ``.venv`` layout
    used by local dev.
    """
    spec = importlib.util.find_spec("pgserver")
    if spec is not None and spec.origin:
        candidate = Path(spec.origin).resolve().parent / "pginstall" / "bin"
        if candidate.exists():
            return candidate
    return (
        PROJECT_ROOT
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "pgserver"
        / "pginstall"
        / "bin"
    )


PGDATA = PROJECT_ROOT / ".spike" / "pgdata"
SOCK_DIR = PROJECT_ROOT / ".spike" / "sock"
LOG_FILE = PROJECT_ROOT / ".spike" / "pg.log"
PORT = 5433
DB_USER = "postgres"


def _bin(name: str) -> str:
    """Return a PG binary path, preferring one already on PATH."""
    found = shutil.which(name)
    if found:
        return found
    candidate = _pg_bin_dir() / name
    if candidate.exists():
        return str(candidate)
    return name  # let subprocess raise FileNotFoundError with a clear message


def _pg_ctl(*args: str) -> int:
    """Run ``pg_ctl`` against this instance's data dir.

    Deliberately no ``-k``: ``pg_ctl`` has no such option (it belongs to
    ``postgres`` itself — the socket dir is passed through ``-o`` below), and
    ``pg_ctl status`` locates the server via ``postmaster.pid`` + ``/proc``
    rather than through the socket.
    """
    cmd = [_bin("pg_ctl"), "-D", str(PGDATA), *args]
    return subprocess.run(cmd).returncode


def _cmd(*args: str) -> None:
    """Run a PG binary inheriting stdio (no check — caller reads return code)."""
    subprocess.run(args)


def _psql(database: str, sql: str) -> str:
    """Run a single psql ``-tAc`` query against *database*."""
    out = subprocess.run(
        [
            _bin("psql"),
            "-h", str(SOCK_DIR),
            "-p", str(PORT),
            "-U", DB_USER,
            "-d", database,
            "-tAc", sql,
        ],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _ensure_dirs() -> None:
    SOCK_DIR.mkdir(parents=True, exist_ok=True)
    PGDATA.mkdir(parents=True, exist_ok=True)


def _server_answers() -> bool:
    """Can the server actually be reached over its socket?

    The liveness probe that does NOT depend on ``/proc``: ``pg_ctl status``
    reads ``postmaster.pid`` and then checks that the PID is alive, which fails
    inside a container / PID namespace (``--unshare-pid``) that cannot see the
    postmaster even though the server is up and serving.  A real connection
    settles the question for both cases.
    """
    rc = subprocess.run(
        [
            _bin("psql"),
            "-h", str(SOCK_DIR),
            "-p", str(PORT),
            "-U", DB_USER,
            "-d", "postgres",
            "-tAc", "SELECT 1",
        ],
        capture_output=True,
        text=True,
    ).returncode
    return rc == 0


def _server_opts() -> list[str]:
    # The socket directory (-k) MUST be under .spike/ (sandbox constraint).
    return ["-k", str(SOCK_DIR), "-p", str(PORT)]


def cmd_start() -> int:
    _ensure_dirs()
    # initdb if this is a fresh data dir (no PG_VERSION marker).
    if not (PGDATA / "PG_VERSION").exists():
        rc = subprocess.run(
            [_bin("initdb"), "-D", str(PGDATA), "-U", DB_USER, "-E", "UTF8", "--no-locale"]
        ).returncode
        if rc != 0:
            return rc
    rc = _pg_ctl("status")
    if rc == 0:
        print("postgres already running.")
        return 0
    # ``-o`` passes the server options (socket dir, port); ``-w`` waits for the
    # readiness signal.  The start result is NOT swallowed in the normal case
    # (this runs as the systemd unit's ``ExecStartPre``, where a non-zero exit
    # aborts the unit instead of letting it come up without a database).
    rc = _pg_ctl(
        "-o", " ".join(_server_opts()),
        "-l", str(LOG_FILE),
        "-w",
        "start",
    )
    if rc == 0:
        return 0
    # A non-zero start is not proof of failure: ``pg_ctl status`` cannot verify
    # liveness when this runs inside a PID namespace that cannot see the
    # postmaster (``/proc/<pid>`` missing → status exits 3 for a healthy
    # server), and ``start`` then fails with "another server might be running".
    # Fall back to asking the server itself before declaring failure.
    if _server_answers():
        print("postgres already running (pg_ctl could not verify it).")
        return 0
    print(f"pg_ctl start failed (rc={rc})", file=sys.stderr)
    return rc


def cmd_stop() -> int:
    rc = _pg_ctl("status")
    if rc != 0:
        print("postgres is not running.")
        return 0
    _pg_ctl("-w", "stop")
    return 0


def cmd_status() -> int:
    return _pg_ctl("status")


def cmd_createdb(name: str) -> int:
    """Create *name* (skipping if it already exists)."""
    existing = _psql("postgres", "SELECT datname FROM pg_database WHERE datname='" + name + "'")
    if existing == name:
        print(f"database {name!r} already exists.")
        return 0
    _ensure_dirs()
    rc = subprocess.run(
        [_bin("createdb"), "-h", str(SOCK_DIR), "-p", str(PORT), "-U", DB_USER, name]
    ).returncode
    if rc == 0:
        print(f"created database {name!r}")
    return rc


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    action = argv[0]
    if action == "start":
        return cmd_start()
    if action == "stop":
        return cmd_stop()
    if action == "status":
        return cmd_status()
    if action == "createdb":
        if len(argv) < 2:
            print("usage: pg_dev.py createdb <name>", file=sys.stderr)
            return 1
        return cmd_createdb(argv[1])
    print(f"unknown action: {action}", file=sys.stderr)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
