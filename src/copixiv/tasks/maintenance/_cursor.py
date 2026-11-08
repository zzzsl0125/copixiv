"""Resume cursors for bounded, resumable maintenance tasks.

Every maintenance task that processes the table in capped slices stores its
own resume point in a ``setting`` row, so a timeout costs a *slice*, not the
whole pass.  The helpers are shared — the keys are not: each task (and each
``fix_series_index`` branch) passes its own key.

Split out of ``copixiv.tasks.maintenance`` (see that package's docstring).

The helpers stay coroutines because their callers are task coroutines, but
each one crosses into the synchronous data layer exactly once:
:func:`copixiv.db.write_lock.read_transaction` for the read,
:func:`copixiv.db.write_lock.run_write_transaction` for the write.
"""

from copixiv.db.write_lock import read_transaction, run_write_transaction


_CURSOR_KEY = "check_epub.cursor"


def _read_cursor_sync(uow, key: str) -> int:
    """Synchronous body of :func:`_read_cursor` (runs in a worker thread)."""
    import json

    from sqlalchemy import select as _select
    from copixiv.db import models

    value = uow.session.execute(
        _select(models.Setting.value).where(
            models.Setting.key == key
        )
    ).scalar()
    if not value:
        return 0
    try:
        return int(json.loads(value))
    except (TypeError, ValueError):
        return 0


async def _read_cursor(uow, key: str = _CURSOR_KEY) -> int:
    """Where the last sweep stopped (0 on the first pass or after wrap-around).

    *key* is the ``setting`` row that holds this task's own resume point:
    every bounded, resumable task must pass its own key — sharing
    ``_CURSOR_KEY`` across tasks would let one task's progress overwrite
    another's.
    """
    return await read_transaction(uow, lambda uw: _read_cursor_sync(uw, key))


async def _write_cursor(uow, value: int, key: str = _CURSOR_KEY) -> None:
    """Persist the resume point (its own short transaction).

    Called *after* the corresponding rows are committed, never before: a
    crash must only re-run a batch, never skip one.
    """
    import json

    from sqlalchemy import delete as _delete

    from copixiv.db import models

    payload = json.dumps(value)

    def _store(uw) -> None:
        session = uw.session
        session.execute(
            _delete(models.Setting).where(models.Setting.key == key)
        )
        session.add(models.Setting(key=key, value=payload))

    await run_write_transaction(uow, _store)
