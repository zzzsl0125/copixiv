"""``@offload`` — run a sync implementation in a worker thread.

Repositories are written synchronously (SQLAlchemy ``Session`` is not async)
but expose an ``async`` API.  The long-standing shape was a pair per method::

    async def get_novels(self, spec):
        return await asyncio.to_thread(self._get_novels_sync, spec)

    def _get_novels_sync(self, spec): ...

That doubles every public name and lets the two drift.  ``@offload`` keeps
the *same* sync body under the public name and makes the public callable a
coroutine that awaits ``asyncio.to_thread``::

    @offload
    def get_novels(self, spec): ...

The decorated public name is a coroutine function; the original sync body
stays reachable as ``wrapper.__wrapped__`` (``functools.wraps``) for tests
that want to call it directly.  A pure stdlib wrapper — no executor, no
framework, no behaviour change.
"""

import asyncio
import functools


def offload(func):
    """Wrap *func* so the returned coroutine runs it via ``asyncio.to_thread``."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


__all__ = ["offload"]
