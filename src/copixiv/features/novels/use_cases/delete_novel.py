"""Use case: delete a novel and its associated files."""

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.storage.file_storage import FileStorage


class DeleteNovelUseCase:
    """Delete a novel from the database and remove its files from disk.

    Looks up the novel internally — the endpoint only needs to pass the ID.
    """

    def __init__(self, uow: SqlUnitOfWork, file_storage: FileStorage):
        self._uow = uow
        self._file_storage = file_storage

    def execute(self, novel_id: int) -> None:
        """Delete a novel by ID, then clean up its files best-effort.

        DB first (committed), files second: a failed DB delete never removes
        files on disk.

        Raises:
            NotFoundError: If the novel doesn't exist.
        """

        def _delete_tx(tx: SqlUnitOfWork) -> str | None:
            deleted, novel_path = SQLAlchemyNovelRepository(tx.session).delete(novel_id)
            if not deleted:
                raise NotFoundError(f"Novel {novel_id} not found")
            return novel_path

        novel_path = run_write_transaction_sync(self._uow, _delete_tx)

        if novel_path:
            # Best-effort cleanup — a failure here must not fail the API
            # call (the row is already gone).
            try:
                self._file_storage.delete_novel_files(novel_path)
            except OSError:
                logger.warning("删除小说文件失败: {}", novel_path, exc_info=True)
