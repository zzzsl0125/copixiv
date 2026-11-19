"""Use case: delete a novel and its associated files."""

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
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
            repo = SQLAlchemyNovelRepository(tx.session)
            novel = repo.get_by_id(novel_id)
            if not novel:
                raise NotFoundError(f"Novel {novel_id} not found")
            repo.delete(novel_id)
            return novel.path

        novel_path = run_write_transaction_sync(self._uow, _delete_tx)

        if novel_path:
            # Best-effort cleanup — a failure here must not fail the API
            # call (the row is already gone).
            self._file_storage.delete_novel_files(novel_path)
