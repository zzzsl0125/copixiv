"""Database backup using SQLite VACUUM INTO (SQLite 3.27+).

Provides daily backup creation and retention cleanup (7 days).
"""

from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine


def backup_database(
    database_path: str,
    backup_dir: str | None = None,
    engine: Engine | None = None,
) -> str:
    """Create a backup of the SQLite database using VACUUM INTO.

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

    today = date.today().isoformat()
    dest = backup_path / f"{today}.db"

    # Remove existing same-day backup so VACUUM INTO can write fresh
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
    keep_days: int = 7,
    backup_dir: str | None = None,
) -> list[str]:
    """Remove backup files older than *keep_days*.

    Args:
        database_path: Path to the source database file (used to locate backups).
        keep_days: Number of days of backups to retain.
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

    cutoff = date.today() - timedelta(days=keep_days)
    removed = []

    for f in backup_path.glob("*.db"):
        try:
            file_date = date.fromisoformat(f.stem)
            if file_date < cutoff:
                f.unlink()
                removed.append(str(f))
        except (ValueError, OSError):
            # f.stem is not a date — skip
            pass

    return removed
