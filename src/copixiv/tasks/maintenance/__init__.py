"""Maintenance tasks — thin ``@register`` adapters (general lane)."""

from .backup import BackupDatabaseArgs, backup_database
from .epub import check_epub
from .files import PruneOrphanFilesArgs, prune_orphan_files
from .series import FixSeriesIndexArgs, fix_series_index

__all__ = [
    "backup_database",
    "BackupDatabaseArgs",
    "check_epub",
    "fix_series_index",
    "FixSeriesIndexArgs",
    "prune_orphan_files",
    "PruneOrphanFilesArgs",
]
