"""Tests for database backup creation and rotation (previously 0% covered)."""

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text

from copixiv.infrastructure.database.backup import (
    backup_database,
    cleanup_old_backups,
)


def _make_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO t (id, name) VALUES (1, 'hello')"))
    engine.dispose()


class TestBackupDatabase:
    def test_backup_contains_current_data(self, tmp_path):
        db = tmp_path / "app.db"
        _make_db(db)

        dest = backup_database(str(db), backup_dir=str(tmp_path / "backups"))

        assert Path(dest).exists()
        with sqlite3.connect(dest) as conn:
            rows = conn.execute("SELECT id, name FROM t").fetchall()
        assert rows == [(1, "hello")]

    def test_same_week_backup_is_replaced(self, tmp_path):
        db = tmp_path / "app.db"
        _make_db(db)

        first = backup_database(str(db), backup_dir=str(tmp_path / "backups"))
        second = backup_database(str(db), backup_dir=str(tmp_path / "backups"))

        assert first == second  # same ISO-week name
        assert Path(second).exists()
        # No stale .db siblings other than the single weekly file.
        assert sorted(p.name for p in Path(first).parent.glob("*.db")) == [
            Path(first).name,
        ]


class TestCleanupOldBackups:
    def test_keeps_newest_by_name(self, tmp_path):
        db = tmp_path / "app.db"
        _make_db(db)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for week in ("W01", "W02", "W03", "W04", "W05"):
            (backup_dir / f"2026-{week}.db").write_bytes(b"x")

        removed = cleanup_old_backups(str(db), keep_count=2,
                                      backup_dir=str(backup_dir))

        assert len(removed) == 3
        remaining = sorted(p.name for p in backup_dir.glob("*.db"))
        assert remaining == ["2026-W04.db", "2026-W05.db"]

    def test_missing_backup_dir_is_noop(self, tmp_path):
        removed = cleanup_old_backups(
            str(tmp_path / "nope.db"),
            backup_dir=str(tmp_path / "no-such-backups"),
        )
        assert removed == []
