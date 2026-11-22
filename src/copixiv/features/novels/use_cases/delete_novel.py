"""Use case: delete a novel and its associated files."""

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.storage.file_storage import FileStorage


def delete_novel(
    novel_id: int,
    *,
    uow: SqlUnitOfWork,
    file_storage: FileStorage,
) -> None:
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

    novel_path = run_write_transaction_sync(uow, _delete_tx)

    if novel_path:
        # Best-effort cleanup — a failure here must not fail the API
        # call (the row is already gone).
        try:
            file_storage.delete_novel_files(novel_path)
        except OSError:
            logger.warning("删除小说文件失败: {}", novel_path, exc_info=True)
