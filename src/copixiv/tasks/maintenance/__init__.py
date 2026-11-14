"""Maintenance tasks — data repair, consistency checks, and index rebuilds.

Registered task functions that fix up stale or missing data.  They only
depend on the database and (optionally) the Pixiv API client, all reached
through the :class:`TaskContext` (docs/MODULARITY.md §M8).

All tasks return a :class:`TaskResult` with a human-readable summary.
Since these are maintenance tasks (not novel discovery), the
``new_novel_titles`` list is always empty — the notifier will send a
plain summary instead of incorrectly labelling results as "new novels".

**Layout.**  This used to be one 1 093-line module carrying five unrelated
tasks and one 430-line function.  The bodies moved *verbatim* — no logic
edits, every docstring and comment kept, because they encode incident
knowledge — into four task modules plus a shared cursor module:

* :mod:`.epub` — ``check_epub`` + its disk-scan helpers (``_sweep``,
  ``_scan_placeholder``, ``_STALE_DAYS``, …).
* :mod:`.search` — ``sync_empty_name``, ``rebuild_fts``, ``check_fts``.
* :mod:`.series` — ``fix_series_index`` + ``FixSeriesIndexArgs`` and the
  series-only page/draft helpers and branch cursors.
* :mod:`._cursor` — ``_read_cursor`` / ``_write_cursor`` / ``_CURSOR_KEY``,
  shared by epub and series.  The *keys* stay per-task: sharing a key
  between tasks would let one task's progress overwrite another's.
* :mod:`.files` — ``prune_orphan_files``: walks the *disk* and asks "is there
  a row for this file?", the opposite direction from ``check_epub`` (which
  walks rows).  Kept separate so a bug there cannot touch the EPUB state
  machine.
* :mod:`.history` — ``prune_task_history``: trims the audit/queue table by
  "keep newest N rows **and** keep the last N days" (dry-run by default).
* :mod:`.refetch` — ``refetch_missing_source``: re-downloads novels whose text
  file vanished (``has_epub=4``).  The reconciler only *labels* them; nothing
  was fetching them back.
* :mod:`.backup` — ``backup_database``: the weekly ``pg_dump``, moved out of
  ``create_app()``'s startup path.  An enabled schedule for it also disables
  the startup fallback (see ``copixiv.app._has_enabled_backup_schedule``).

Importing this package executes every ``@register(...)`` decorator, because
the re-exports below import all four task modules — that is what keeps
:data:`copixiv.tasks.kernel.DEFAULT_TASK_MODULES` (which names
``"copixiv.tasks.maintenance"``) working unchanged.

**Patching contract.** The names below are re-binds, not the live objects:
``check_epub`` reads ``_sweep`` / ``_scan_placeholder``
from :mod:`.epub`'s own module globals. A test that must change ``check_epub``'s
behaviour therefore patches :mod:`copixiv.tasks.maintenance.epub`, not this
package — patching here would only replace the re-exported shadow copy and
leave the real consumer untouched. Reads through this package stay valid
(``maintenance._scan_placeholder(...)`` works for inspection).
"""

from .backup import BackupDatabaseArgs, backup_database
from .epub import check_epub
from .files import PruneOrphanFilesArgs, prune_orphan_files
from .history import (
    RETENTION_SETTING_KEY,
    PruneTaskHistoryArgs,
    prune_task_history,
)
from .refetch import RefetchMissingSourceArgs, refetch_missing_source
from .search import check_fts, rebuild_fts, sync_empty_name
from .series import FixSeriesIndexArgs, fix_series_index

__all__ = [
    "backup_database",
    "BackupDatabaseArgs",
    "check_epub",
    "check_fts",
    "rebuild_fts",
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
