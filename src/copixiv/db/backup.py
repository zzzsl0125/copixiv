"""Database backup using ``pg_dump -Fc`` (PostgreSQL custom format).

postgres-migration: SQLite's ``VACUUM INTO`` is gone.  Weekly backups are
now ``pg_dump`` custom-format dumps named after the current ISO week (e.g.
``2026-W27.dump``), with the same retention policy via
:func:`cleanup_old_backups` (which globs ``*.dump``).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)


def _pg_bin(name: str) -> str:
    """Return the path to a PostgreSQL admin binary.

    Prefers ``pgserver``-bundled binaries (matching ``scripts/pg_dev.py``) so
    versions/extensions stay consistent; set ``COPIXIV_USE_SYSTEM_PG=1`` to
    prefer binaries on ``PATH``. Falls back to ``PATH`` if ``pgserver`` is not
    installed.
    """
    if os.environ.get("COPIXIV_USE_SYSTEM_PG") == "1":
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
    resolved = shutil.which(name)
    if resolved:
        return resolved
    return name  # let subprocess raise FileNotFoundError with a clear message


def _connection_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Split a SQLAlchemy URL into ``pg_dump`` command args + env overrides."""
    url = make_url(database_url)

    args: list[str] = []
    env: dict[str, str] = {}

    host: str | None = url.host
    # Socket connections put the socket dir in the ``host`` query parameter.
    if host is None and "host" in url.query:
        query_host = url.query["host"]
        host = (query_host[0] if query_host else None) if isinstance(query_host, tuple) else query_host
    if host:
        args += ["--host", host]

    port: int | str | tuple[str, ...] | None = url.port
    if port is None:
        query_port = url.query.get("port")
        port = query_port[0] if isinstance(query_port, tuple) and query_port else query_port
    args += ["--port", str(port or "5432")]

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
    *,
    timeout: float = 600,
) -> str:
    """Create a ``pg_dump -Fc`` backup of the PostgreSQL database.

    The backup file is named after the current ISO week (e.g. ``2026-W27.dump``).

    Args:
        database_url: PostgreSQL SQLAlchemy URL.
        backup_dir: Directory for backups.  Defaults to ``./backups``.
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
    keep_count: int = 1,
    backup_dir: str | None = None,
) -> list[str]:
    """Remove old ``*.dump`` backups, keeping only the most recent *keep_count*.

    Args:
        keep_count: Number of most-recent backups to retain (default 1, must be >= 1).
        backup_dir: Directory for backups.  Defaults to ``./backups``.

    Returns:
        List of removed file paths.
    """
    if keep_count < 1:
        raise ValueError(f"keep_count must be >= 1, got {keep_count}")

    backup_path = Path(backup_dir or "backups")
    if not backup_path.exists():
        return []

    backups = sorted(backup_path.glob("*.dump"), key=lambda f: f.name, reverse=True)
    removed: list[str] = []

    for f in backups[keep_count:]:
        try:
            f.unlink()
            removed.append(str(f))
        except OSError as exc:
            logger.warning("Failed to remove old backup %s: %s", f, exc)

    return removed
