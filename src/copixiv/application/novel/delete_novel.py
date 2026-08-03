"""Use case: delete a novel and its associated files."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.ports.repositories import NovelRepository
from copixiv.domain.ports.storage import FileStoragePort


class DeleteNovelUseCase:
    """Delete a novel from the database and remove its files from disk.

    Looks up the novel internally — the endpoint only needs to pass the ID.
    """

    def __init__(self, novel_repo: NovelRepository, file_storage: FileStoragePort):
        self._repo = novel_repo
        self._file_storage = file_storage

    async def execute(self, novel_id: int) -> None:
        """Delete a novel by ID, cleaning up associated files first.

        Raises:
            NotFoundError: If the novel doesn't exist.
        """
        novel = await self._repo.get_by_id(novel_id)
        if not novel:
            raise NotFoundError(f"Novel {novel_id} not found")

        if novel_path := novel.get("path"):
            self._file_storage.delete_novel_files(novel_path)

        await self._repo.delete(novel_id)
