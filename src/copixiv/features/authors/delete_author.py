"""Use case: delete an author, all their novels, files on disk, and follow link."""

from __future__ import annotations

import asyncio

from copixiv.core.models import TaskResult
from copixiv.db.tx import run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage


class DeleteAuthorUseCase:
    """Delete an author and all their stored data from DB, disk, and Pixiv follow list."""

    def __init__(
        self,
        uow: SqlUnitOfWork,
        file_storage: FileStorage,
        client: PixivClient,
    ):
        self._uow = uow
        self._file_storage = file_storage
        self._client = client

    async def execute(self, author_id: int) -> TaskResult:
        """Execute author deletion across PG, disk, and Pixiv."""
        paths = await run_write_transaction(
            self._uow,
            lambda uw: SQLAlchemyAuthorRepository(uw.session).delete_author_and_data(author_id),
        )

        for path in paths:
            await asyncio.to_thread(self._file_storage.delete_novel_files, path)

        async with self._client.account_rule(force_follow=True):
            await self._client.user_follow_delete(author_id)

        return TaskResult(summary=f"已删除作者 #{author_id} 及其全部小说")
