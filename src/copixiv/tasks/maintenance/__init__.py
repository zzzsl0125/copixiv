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
  ``_scan_placeholder``, ``_MAX_ROWS_PER_RUN``, ``_STALE_DAYS``, …).
* :mod:`.search` — ``sync_empty_name``, ``rebuild_fts``, ``check_fts``.
* :mod:`.series` — ``fix_series_index`` + ``FixSeriesIndexArgs`` and the
  series-only page/draft helpers and branch cursors.
* :mod:`.tags` — ``rebuild_tag_counts``.
* :mod:`._cursor` — ``_read_cursor`` / ``_write_cursor`` / ``_CURSOR_KEY``,
  shared by epub and series.  The *keys* stay per-task: sharing a key
  between tasks would let one task's progress overwrite another's.

Importing this package executes every ``@register(...)`` decorator, because
the re-exports below import all four task modules — that is what keeps
:data:`copixiv.tasks.kernel.DEFAULT_TASK_MODULES` (which names
``"copixiv.tasks.maintenance"``) working unchanged.

**Patching contract.** The names below are re-binds, not the live objects:
``check_epub`` reads ``_MAX_ROWS_PER_RUN`` / ``_sweep`` / ``_scan_placeholder``
from :mod:`.epub`'s own module globals. A test that must change ``check_epub``'s
behaviour therefore patches :mod:`copixiv.tasks.maintenance.epub`, not this
package — patching here would only replace the re-exported shadow copy and
leave the real consumer untouched. Reads through this package stay valid
(``maintenance._scan_placeholder(...)`` works for inspection).
"""

from ._cursor import _CURSOR_KEY, _read_cursor, _write_cursor
from .epub import (
    _MAX_ROWS_PER_RUN,
    _STALE_DAYS,
    _SWEEP_CHUNK,
    _no_images_ever_and_stale,
    _scan_placeholder,
    _sweep,
    _txt_has_images,
    check_epub,
)
from .search import check_fts, rebuild_fts, sync_empty_name
from .series import (
    _ORPHAN_CURSOR_KEY,
    _SERIES_CURSOR_KEY,
    _SWEEP_CURSOR_KEY,
    FixSeriesIndexArgs,
    _draft_from_row,
    _page,
    _stored_columns,
    fix_series_index,
)
from .tags import rebuild_tag_counts

__all__ = [
    "check_epub",
    "check_fts",
    "rebuild_fts",
    "sync_empty_name",
    "fix_series_index",
    "FixSeriesIndexArgs",
    "rebuild_tag_counts",
    "_scan_placeholder",
    "_sweep",
    "_read_cursor",
    "_write_cursor",
    "_ORPHAN_CURSOR_KEY",
    "_SERIES_CURSOR_KEY",
    "_SWEEP_CURSOR_KEY",
]
