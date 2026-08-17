"""Write-lock port — serializes SQLite write transactions.

The concrete implementation lives in
``infrastructure/database/write_lock.py``; application-layer use cases
depend only on this protocol (injected by the task runner), keeping the
dependency direction ``application → domain ← infrastructure`` intact.
"""

from collections.abc import AsyncIterator
from typing import Protocol


class WriteLockPort(Protocol):
    """A callable returning an async context manager that serializes writes."""

    def __call__(self) -> AsyncIterator[None]: ...
