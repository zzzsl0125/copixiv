"""Process-wide data-version epoch ("提交即失效").

The single exit point for any data change is a transaction commit:
``Session.commit()``.  Every in-process data cache compares its snapshot
against ``current_epoch()`` and treats a changed epoch as "stale".

The hook is registered here, at module import, via
``sqlalchemy.event.listens_for(sqlalchemy.orm.Session, "after_commit")``.
Registration is module-scoped (Python caches the module, so importing
this package from many places never double-registers the listener); once
registered it is global for the whole process — every successful commit
that actually mutated data bumps the epoch, and any cached value
recorded under an older epoch is dropped on the next read.

Because a single-writer instance lock (S2e) guarantees at most one
writer per process, this process-local hook is sufficient: there is no
need for cross-process version coordination.

**Only real mutations bump the epoch.**  ``Session.commit()`` is also
issued by *read-only* request paths (``get_uow`` commits on clean
exit), so bumping on every commit would invalidate every cached value
after each request — the count cache would never hit.  A cursor-level
probe (installed by :func:`install_dml_probe` on each engine) records
whether the transaction actually executed DML (``INSERT``/``UPDATE``/
``DELETE``/``COPY`` — ORM *and* bulk statements, since the write
repositories use core ``update()``/``delete()`` heavily);
``after_commit`` bumps only when that probe fired; an empty commit (a
common read request) leaves the epoch untouched, and a rollback clears
the flag.

Usage::

    from copixiv.db.data_version import current_epoch
    cache[key] = (current_epoch(), value)
    cached_epoch, value = cache[key]
    if cached_epoch == current_epoch():
        return value
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

_epoch: int = 0
_pending_mutation: bool = False

# Statement prefixes that count as a real mutation of the transaction.
_DML_PREFIXES = ("insert", "update", "delete", "merge", "copy", "truncate")


def current_epoch() -> int:
    """Return the current data version.

    A cache entry is valid only while its recorded epoch equals the value
    returned here; after a successful commit that actually changed data
    the value differs.
    """
    return _epoch


@event.listens_for(Engine, "before_cursor_execute")
def _probe_dml(conn, cursor, statement, parameters, context, executemany) -> None:
    """Mark the transaction as dirty when a DML statement runs."""
    global _pending_mutation
    if _pending_mutation:
        return
    head = statement.lstrip().lstrip("(").strip().lower()
    if head.startswith(_DML_PREFIXES):
        _pending_mutation = True


@event.listens_for(Session, "after_commit")
def _on_after_commit(session: Session) -> None:
    """Bump the epoch only when the commit actually changed data."""
    global _epoch, _pending_mutation
    if _pending_mutation:
        _epoch += 1
    _pending_mutation = False


@event.listens_for(Session, "after_rollback")
def _on_after_rollback(session: Session) -> None:
    """A rolled-back transaction changed nothing — clear the probe."""
    global _pending_mutation
    _pending_mutation = False
