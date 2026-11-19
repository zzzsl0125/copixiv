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
import sys
import uuid
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
        candidate = Path(spec.origin).resolve().parent / "pginstall" / "bin" / name
        if candidate.exists():
            return str(candidate)
    venv_bin = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
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
        query_host = url.query["host"]
        host = query_host[0] if isinstance(query_host, tuple) else query_host
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
    *,
    timeout: float = 600,
) -> str:
    """Create a ``pg_dump -Fc`` backup of the PostgreSQL database.

    The backup file is named after the current ISO week (e.g. ``2026-W27.dump``).

    Args:
        database_url: PostgreSQL SQLAlchemy URL.
        backup_dir: Directory for backups.  Defaults to ``./backups``.
        engine: Unused — kept for backwards compatibility with the old
            SQLite ``backup_database`` signature.
        timeout: Subprocess timeout in seconds (default 600).

    Returns:
        Path to the created backup file.
    """
    if backup_dir is None:
        backup_dir = "backups"
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    safe_url = make_url(database_url).render_as_string(hide_password=True)
    this_week = date.today().strftime("%G-W%V")
    dest = backup_path / f"{this_week}.dump"
    tmp_dest = backup_path / f".{this_week}.{os.getpid()}.{uuid.uuid4().hex[:8]}.dump.tmp"

    args, env = _connection_args(database_url)
    cmd = [_pg_bin("pg_dump"), "--format=custom", "--file", str(tmp_dest), *args]

    merged_env = {**os.environ, **env}
    try:
        subprocess.run(
            cmd,
            check=True,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        tmp_dest.replace(dest)
    except subprocess.CalledProcessError as e:
        tmp_dest.unlink(missing_ok=True)
        stderr = (e.stderr or "").strip()
        msg = f"pg_dump failed (exit {e.returncode}) for {safe_url}"
        if stderr:
            msg = f"{msg}: {stderr}"
        raise RuntimeError(msg) from e
    except subprocess.TimeoutExpired as e:
        tmp_dest.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump timed out after {timeout}s for {safe_url}") from e
    except BaseException:
        tmp_dest.unlink(missing_ok=True)
        raise

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

    backups = sorted(backup_path.glob("*.dump"), key=lambda f: f.name, reverse=True)
    removed: list[str] = []

    for f in backups[keep_count:]:
        try:
            f.unlink()
            removed.append(str(f))
        except OSError:
            pass

    return removed
