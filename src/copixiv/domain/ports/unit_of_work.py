"""Unit of Work port — transaction boundary."""

from typing import Protocol, runtime_checkable
from collections.abc import AsyncIterator

from .repositories import (
    NovelRepository,
    AuthorRepository,
    SeriesRepository,
    TagRepository,
    TokenRepository,
    TaskRepository,
    SearchHistoryRepository,
)


@runtime_checkable
class UnitOfWork(Protocol):
    """Manages a transactional boundary around repository operations.

    FastAPI endpoints use Depends(get_db) for request-scoped sessions.
    Background tasks use `async with uow.begin()` for explicit transactions.
    """

    novels: NovelRepository
    authors: AuthorRepository
    series: SeriesRepository
    tags: TagRepository
    tokens: TokenRepository
    tasks: TaskRepository
    search_history: SearchHistoryRepository

    def begin(self) -> AsyncIterator[None]: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
