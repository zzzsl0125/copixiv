"""Database backup using SQLite VACUUM INTO (SQLite 3.27+).

Provides weekly backup creation — keeps only the single most recent backup.
"""

from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine


def backup_database(
    database_path: str,
    backup_dir: str | None = None,
    engine: Engine | None = None,
) -> str:
    """Create a backup of the SQLite database using VACUUM INTO.

    The backup file is named after the current ISO week (e.g. ``2026-W27.db``).

    Args:
        database_path: Path to the source SQLite database file.
        backup_dir: Directory for backups.  Defaults to ``<db_dir>/backups/``.
        engine: Optional existing engine to use.  Creates a temporary one if None.

    Returns:
        Path to the created backup file.
    """
    db_path = Path(database_path)
    if backup_dir is None:
        backup_dir = str(db_path.parent / "backups")

    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    this_week = date.today().strftime("%G-W%V")
    dest = backup_path / f"{this_week}.db"

    # Remove existing same-week backup so VACUUM INTO can write fresh
    if dest.exists():
        dest.unlink()

    if engine is None:
        from sqlalchemy import create_engine
        engine = create_engine(f"sqlite:///{database_path}")

    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        conn.execute(text(f"VACUUM INTO '{dest}'"))
        conn.commit()

    return str(dest)


def cleanup_old_backups(
    database_path: str,
    keep_count: int = 1,
    backup_dir: str | None = None,
) -> list[str]:
    """Remove old backup files, keeping only the most recent *keep_count*.

    Args:
        database_path: Path to the source database file (used to locate backups).
        keep_count: Number of most-recent backups to retain (default 1).
        backup_dir: Directory for backups.  Defaults to ``<db_dir>/backups/``.

    Returns:
        List of removed file paths.
    """
    db_path = Path(database_path)
    if backup_dir is None:
        backup_dir = str(db_path.parent / "backups")

    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return []

    backups = sorted(backup_path.glob("*.db"), key=lambda f: f.name, reverse=True)
    removed = []

    for f in backups[keep_count:]:
        try:
            f.unlink()
            removed.append(str(f))
        except OSError:
            pass

    return removed
