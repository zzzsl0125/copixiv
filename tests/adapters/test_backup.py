"""Tests for PostgreSQL backup creation and rotation (pg_dump -Fc)."""

import subprocess
from pathlib import Path

from copixiv.db.backup import (
    _pg_bin,
    backup_database,
    cleanup_old_backups,
)

TEST_URL = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test"


def _list_dump(path: Path) -> str:
    """Run ``pg_restore -l`` on a custom-format dump; return its stdout."""
    out = subprocess.run(
        [_pg_bin("pg_restore"), "--list", str(path)],
        capture_output=True, text=True,
    )
    return out.stdout


class TestBackupDatabase:
    def test_backup_contains_current_data(self, tmp_path):
        dest = backup_database(TEST_URL, backup_dir=str(tmp_path / "backups"))

        assert Path(dest).exists()
        assert Path(dest).stat().st_size > 0
        # The dump must list the application tables (so it actually holds data).
        listing = _list_dump(Path(dest))
        assert "TABLE public novel" in listing
        assert "TABLE public author" in listing

    def test_same_week_backup_is_replaced(self, tmp_path):
        first = backup_database(TEST_URL, backup_dir=str(tmp_path / "backups"))
        second = backup_database(TEST_URL, backup_dir=str(tmp_path / "backups"))

        assert first == second  # same ISO-week name
        assert Path(second).exists()
        assert sorted(p.name for p in Path(first).parent.glob("*.dump")) == [
            Path(first).name,
        ]


class TestCleanupOldBackups:
    def test_keeps_newest_by_name(self, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for week in ("W01", "W02", "W03", "W04", "W05"):
            (backup_dir / f"2026-{week}.dump").write_bytes(b"x")

        removed = cleanup_old_backups(
            TEST_URL, keep_count=2, backup_dir=str(backup_dir),
        )

        assert len(removed) == 3
        remaining = sorted(p.name for p in backup_dir.glob("*.dump"))
        assert remaining == ["2026-W04.dump", "2026-W05.dump"]

    def test_missing_backup_dir_is_noop(self, tmp_path):
        removed = cleanup_old_backups(
            TEST_URL,
            backup_dir=str(tmp_path / "no-such-backups"),
        )
        assert removed == []
