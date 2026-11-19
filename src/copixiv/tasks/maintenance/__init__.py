"""Maintenance tasks — thin ``@register`` adapters (general lane)."""

from .backup import BackupDatabaseArgs, backup_database
from .epub import check_epub
from .files import PruneOrphanFilesArgs, prune_orphan_files
from .history import (
    RETENTION_SETTING_KEY,
    PruneTaskHistoryArgs,
    prune_task_history,
)
from .refetch import RefetchMissingSourceArgs, refetch_missing_source
from .search import sync_empty_name
from .series import FixSeriesIndexArgs, fix_series_index

__all__ = [
    "backup_database",
    "BackupDatabaseArgs",
    "check_epub",
    "sync_empty_name",
    "fix_series_index",
    "FixSeriesIndexArgs",
    "prune_orphan_files",
    "PruneOrphanFilesArgs",
    "prune_task_history",
    "PruneTaskHistoryArgs",
    "refetch_missing_source",
    "RefetchMissingSourceArgs",
    "RETENTION_SETTING_KEY",
]
