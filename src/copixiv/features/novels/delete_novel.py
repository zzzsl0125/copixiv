"""Use case: delete a novel and its associated files."""

from copixiv.core.exceptions import NotFoundError
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.storage.file_storage import FileStorage


class DeleteNovelUseCase:
    """Delete a novel from the database and remove its files from disk.

    Looks up the novel internally — the endpoint only needs to pass the ID.
    """

    def __init__(self, novel_repo: SQLAlchemyNovelRepository, file_storage: FileStorage):
        self._repo = novel_repo
        self._file_storage = file_storage

    def execute(self, novel_id: int) -> None:
        """Delete a novel by ID, then clean up its files best-effort.

        DB first, files second: a failed DB delete must not leave the
        database pointing at files that no longer exist (a dangling row
        is worse than orphaned files — orphans are easy to spot and the
        weekly ``check_epub``-style sweeps can reclaim them).

        Pure DB + a best-effort local file delete: synchronous, so the
        endpoint declaring it is a plain ``def`` and FastAPI runs the whole
        thing (including the blocking ``Session`` work) in its worker thread.

        Raises:
            NotFoundError: If the novel doesn't exist.
        """
        novel = self._repo.get_by_id(novel_id)
        if not novel:
            raise NotFoundError(f"Novel {novel_id} not found")

        novel_path = novel.path
        self._repo.delete(novel_id)

        if novel_path:
            # Best-effort cleanup — a failure here must not fail the API
            # call (the row is already gone).
            self._file_storage.delete_novel_files(novel_path)
