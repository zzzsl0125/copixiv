"""Use case: delete a novel and its associated files."""

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.database import models
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.storage.file_storage import FileStorage


class DeleteNovelUseCase:
    """Delete a novel from the database and remove its files from disk.

    Looks up the novel internally — the endpoint only needs to pass the ID.
    """

    def __init__(self, novel_repo: NovelRepository, file_storage: FileStorage):
        self._repo = novel_repo
        self._file_storage = file_storage

    async def execute(self, novel_id: int) -> None:
        """Delete a novel by ID, cleaning up associated files first.

        Raises:
            NotFoundError: If the novel doesn't exist.
        """
        novel = self._repo.session.get(models.Novel, novel_id)
        if not novel:
            raise NotFoundError(f"Novel {novel_id} not found")

        if novel.path:
            self._file_storage.delete_novel_files(novel.path)

        await self._repo.delete(novel_id)
