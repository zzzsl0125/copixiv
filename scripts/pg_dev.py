#!/usr/bin/env python3
"""Local PostgreSQL dev/service instance manager for copixiv.

Wraps the ``pgserver``-bundled PostgreSQL 16 binaries (defaulting to
``pgserver`` so extension paths stay consistent; set
``COPIXIV_USE_SYSTEM_PG=1`` to prefer binaries on ``PATH``).  The data
directory and Unix socket live under ``data/pg/`` and logs under ``log/``.

Usage::

    python scripts/pg_dev.py start               # boot background instance
    python scripts/pg_dev.py run                 # exec foreground postgres (systemd)
    python scripts/pg_dev.py status              # is it running?
    python scripts/pg_dev.py createdb <name>     # create a database
    python scripts/pg_dev.py stop                # stop the instance (keeps data)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PGDATA = PROJECT_ROOT / "data" / "pg" / "pgdata"
SOCK_DIR = PROJECT_ROOT / "data" / "pg" / "sock"
LOG_FILE = PROJECT_ROOT / "log" / "pg.log"
PORT = 5433
DB_USER = "postgres"

_DB_NAME_RE = re.compile(r"[A-Za-z0-9_]+")


def _pg_bin_dir() -> Path:
    """Return pgserver's bundled ``bin`` directory."""
    spec = importlib.util.find_spec("pgserver")
    if spec is not None and spec.origin:
        candidate = Path(spec.origin).resolve().parent / "pginstall" / "bin"
        if candidate.exists():
            return candidate
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return PROJECT_ROOT / ".venv" / "lib" / py_ver / "site-packages" / "pgserver" / "pginstall" / "bin"


def _bin(name: str) -> str:
    """Return a PG binary path, preferring ``pgserver`` unless overridden."""
    if os.environ.get("COPIXIV_USE_SYSTEM_PG") == "1":
        found = shutil.which(name)
        if found:
            return found
    candidate = _pg_bin_dir() / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    return name  # let subprocess raise FileNotFoundError with a clear message


def _pg_ctl(*args: str) -> int:
    """Run ``pg_ctl`` against this instance's data dir."""
    cmd = [_bin("pg_ctl"), "-D", str(PGDATA), *args]
    return subprocess.run(cmd).returncode


def _psql_cmd(database: str, *extra_args: str) -> list[str]:
    """Build a ``psql`` command targeting this instance's Unix socket."""
    return [
        _bin("psql"),
        "-h",
        str(SOCK_DIR),
        "-p",
        str(PORT),
        "-U",
        DB_USER,
        "-d",
        database,
        *extra_args,
    ]


def _ensure_dirs() -> None:
    SOCK_DIR.mkdir(parents=True, exist_ok=True)
    PGDATA.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _ensure_initialized() -> int:
    """Initialize PGDATA with ``initdb`` if it does not exist yet."""
    _ensure_dirs()
    if (PGDATA / "PG_VERSION").exists():
        return 0
    return subprocess.run(
        [_bin("initdb"), "-D", str(PGDATA), "-U", DB_USER, "-E", "UTF8", "--no-locale"]
    ).returncode


def _server_answers() -> bool:
    """Can the server actually be reached over its socket?"""
    rc = subprocess.run(
        _psql_cmd("postgres", "-tAc", "SELECT 1"),
        capture_output=True,
        text=True,
    ).returncode
    return rc == 0


def _server_opts() -> list[str]:
    return ["-k", str(SOCK_DIR), "-p", str(PORT)]


def cmd_start() -> int:
    return _start()


def _start() -> int:
    init_rc = _ensure_initialized()
    if init_rc != 0:
        return init_rc
    rc = _pg_ctl("status")
    if rc == 0:
        print("postgres already running.")
        return 0
    rc = _pg_ctl(
        "-o",
        " ".join(_server_opts()),
        "-l",
        str(LOG_FILE),
        "-w",
        "start",
    )
    if rc == 0:
        return 0
    if _server_answers():
        print("postgres already running (pg_ctl could not verify it).")
        return 0
    print(f"pg_ctl start failed (rc={rc})", file=sys.stderr)
    return rc


def cmd_run() -> int:
    """Run ``postgres`` in the foreground (for systemd ``Type=simple``)."""
    init_rc = _ensure_initialized()
    if init_rc != 0:
        return init_rc
    if _pg_ctl("status") == 0 or _server_answers():
        print(
            "postgres is already running; stop the background instance before starting foreground mode.",
            file=sys.stderr,
        )
        return 1
    postgres_bin = _bin("postgres")
    os.execvp(
        postgres_bin,
        [postgres_bin, "-D", str(PGDATA), *_server_opts()],
    )
    return 1  # pragma: no cover


def cmd_stop() -> int:
    if _pg_ctl("status") != 0 and not _server_answers():
        print("postgres is not running.")
        return 0
    return _pg_ctl("-w", "stop")


def cmd_status() -> int:
    rc = _pg_ctl("status")
    if rc == 0:
        return 0
    if _server_answers():
        print("postgres is answering on socket (pg_ctl status exited non-zero).")
        return 0
    return rc


def cmd_createdb(name: str) -> int:
    """Create *name* (skipping if it already exists)."""
    if not _DB_NAME_RE.fullmatch(name):
        print(
            f"invalid database name {name!r}: only [A-Za-z0-9_] allowed.",
            file=sys.stderr,
        )
        return 1
    probe = subprocess.run(
        _psql_cmd("postgres", "-X", "-tA", "-v", f"dbname={name}"),
        input="SELECT datname FROM pg_database WHERE datname = :'dbname';\n",
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip() == name:
        print(f"database {name!r} already exists.")
        return 0
    _ensure_dirs()
    rc = subprocess.run(
        [_bin("createdb"), "-h", str(SOCK_DIR), "-p", str(PORT), "-U", DB_USER, name]
    ).returncode
    if rc == 0:
        print(f"created database {name!r}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local PostgreSQL manager for copixiv.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("start", help="Start PostgreSQL in the background")
    sub.add_parser("run", help="Run PostgreSQL in the foreground (systemd)")
    sub.add_parser("stop", help="Stop the background PostgreSQL instance")
    sub.add_parser("status", help="Check PostgreSQL status")
    createdb_parser = sub.add_parser("createdb", help="Create a database if missing")
    createdb_parser.add_argument("name", help="Database name ([A-Za-z0-9_]+)")

    args = parser.parse_args(argv)
    if args.action == "start":
        return cmd_start()
    if args.action == "run":
        return cmd_run()
    if args.action == "stop":
        return cmd_stop()
    if args.action == "status":
        return cmd_status()
    if args.action == "createdb":
        return cmd_createdb(args.name)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
