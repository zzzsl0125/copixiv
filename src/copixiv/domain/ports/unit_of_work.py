"""Unit of Work port — transaction boundary."""

from collections.abc import AsyncIterator
from typing import Protocol

from .repositories import (
    NovelRepository,
    AuthorRepository,
    SeriesRepository,
    TagRepository,
    TokenRepository,
    TaskRepository,
    SearchHistoryRepository,
    FailedNovelRepositoryPort,
)


class UnitOfWork(Protocol):
    """Manages a transactional boundary around repository operations.

    FastAPI endpoints use Depends(get_db) for request-scoped sessions.
    Background tasks use `async with uow.begin()` for explicit transactions.

    Note: no ``session`` attribute is exposed — the domain layer stays
    free of ORM types; concrete repositories are reached via the
    properties below.
    """

    novels: NovelRepository
    authors: AuthorRepository
    series: SeriesRepository
    tags: TagRepository
    tokens: TokenRepository
    tasks: TaskRepository
    search_history: SearchHistoryRepository
    failed_novels: FailedNovelRepositoryPort

    def begin(self) -> AsyncIterator[None]: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
