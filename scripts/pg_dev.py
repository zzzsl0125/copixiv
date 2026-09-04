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
    subprocess.run(
        [
            _bin("pg_ctl"),
            "-D", str(PGDATA),
            "-o", " ".join(_server_opts()),
            "-l", str(LOG_FILE),
            "-w",
            "start",
        ]
    )
    _pg_ctl("status")
    return 0


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
