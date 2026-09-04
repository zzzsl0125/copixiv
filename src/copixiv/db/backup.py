"""Database backup using ``pg_dump -Fc`` (PostgreSQL custom format).

postgres-migration: SQLite's ``VACUUM INTO`` is gone.  Weekly backups are
now ``pg_dump`` custom-format dumps named after the current ISO week (e.g.
``2026-W27.dump``), with the same retention policy via
:func:`cleanup_old_backups` (which globs ``*.dump``).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

from sqlalchemy.engine import Engine, make_url


def _pg_bin(name: str) -> str:
    """Return the path to a PostgreSQL admin binary.

    Prefers a binary already on ``PATH``; otherwise falls back to the
    ``pgserver``-bundled binaries — first in the *running* interpreter
    (where CI installs it), then the repo-local ``.venv`` layout used by
    the dev/test harness.
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    spec = importlib.util.find_spec("pgserver")
    if spec is not None and spec.origin:
        candidate = (
            Path(spec.origin).resolve().parent / "pginstall" / "bin" / name
        )
        if candidate.exists():
            return str(candidate)
    venv_bin = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "pgserver"
        / "pginstall"
        / "bin"
    )
    candidate = venv_bin / name
    if candidate.exists():
        return str(candidate)
    return name  # let subprocess raise FileNotFoundError with a clear message


def _connection_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Split a SQLAlchemy URL into ``pg_dump`` command args + env overrides."""
    url = make_url(database_url)

    args: list[str] = []
    env: dict[str, str] = {}

    host = url.host
    # Socket connections put the socket dir in the ``host`` query parameter.
    if host is None and "host" in url.query:
        host = url.query["host"]
    if host:
        args += ["--host", host]

    port = url.port or url.query.get("port") or "5432"
    args += ["--port", str(port)]

    if url.username:
        args += ["--username", url.username]

    if url.password:
        env["PGPASSWORD"] = url.password

    if url.database:
        args += ["--dbname", url.database]

    return args, env


def backup_database(
    database_url: str,
    backup_dir: str | None = None,
    engine: Engine | None = None,
) -> str:
    """Create a ``pg_dump -Fc`` backup of the PostgreSQL database.

    The backup file is named after the current ISO week (e.g. ``2026-W27.dump``).

    Args:
        database_url: PostgreSQL SQLAlchemy URL.
        backup_dir: Directory for backups.  Defaults to ``./backups``.
        engine: Unused — kept for backwards compatibility with the old
            SQLite ``backup_database`` signature.

    Returns:
        Path to the created backup file.
    """
    if backup_dir is None:
        backup_dir = "backups"
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    this_week = date.today().strftime("%G-W%V")
    dest = backup_path / f"{this_week}.dump"

    if dest.exists():
        dest.unlink()

    args, env = _connection_args(database_url)
    cmd = [_pg_bin("pg_dump"), "--format=custom", "--file", str(dest), *args]

    merged_env = {**os.environ, **env}
    try:
        subprocess.run(cmd, check=True, env=merged_env)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"pg_dump failed for {database_url!r}: {e}"
        ) from e

    return str(dest)


def cleanup_old_backups(
    database_url: str,
    keep_count: int = 1,
    backup_dir: str | None = None,
) -> list[str]:
    """Remove old ``*.dump`` backups, keeping only the most recent *keep_count*.

    Args:
        database_url: PostgreSQL SQLAlchemy URL (unused for cleanup —
            kept for signature compatibility).
        keep_count: Number of most-recent backups to retain (default 1).
        backup_dir: Directory for backups.  Defaults to ``./backups``.

    Returns:
        List of removed file paths.
    """
    backup_path = Path(backup_dir or "backups")
    if not backup_path.exists():
        return []

    backups = sorted(
        backup_path.glob("*.dump"), key=lambda f: f.name, reverse=True
    )
    removed: list[str] = []

    for f in backups[keep_count:]:
        try:
            f.unlink()
            removed.append(str(f))
        except OSError:
            pass

    return removed
