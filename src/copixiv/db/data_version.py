"""Process-wide data-version epoch ("提交即失效").

The single exit point for any data change is a transaction commit:
``Session.commit()``.  Every in-process data cache compares its snapshot
against ``current_epoch()`` and treats a changed epoch as "stale".

The hook is registered here, at module import, via
``sqlalchemy.event.listens_for(sqlalchemy.orm.Session, "after_commit")``.
Registration is module-scoped (Python caches the module, so importing
this package from many places never double-registers the listener); once
registered it is global for the whole process — every successful commit
bumps the epoch, and any cached value recorded under an older epoch is
dropped on the next read.

Because a single-writer instance lock (S2e) guarantees at most one
writer per process, this process-local hook is sufficient: there is no
need for cross-process version coordination.  (An ``after_commit`` that
fires on a commit with no actual mutation is an acceptable cost — a
single integer increment.)

Usage::

    from copixiv.db.data_version import current_epoch
    cache[key] = (current_epoch(), value)
    cached_epoch, value = cache[key]
    if cached_epoch == current_epoch():
        return value
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

_epoch: int = 0


def current_epoch() -> int:
    """Return the current data version.

    A cache entry is valid only while its recorded epoch equals the value
    returned here; after any ``Session.commit()`` the value differs.
    """
    return _epoch


@event.listens_for(Session, "after_commit")
def _on_after_commit(session: Session) -> None:
    """Bump the epoch after every successful commit."""
    global _epoch
    _epoch += 1
