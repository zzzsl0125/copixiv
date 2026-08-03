"""Use case: retrieve a novel's file path for download."""

from pathlib import Path

from copixiv.domain.exceptions import NotFoundError
from copixiv.domain.ports.repositories import NovelRepository


class GetNovelFileUseCase:
    """Look up a novel and verify its file exists on disk.

    Returns the resolved file path and media type so the HTTP layer can
    serve a ``FileResponse``.
    """

    def __init__(self, novel_repo: NovelRepository):
        self._repo = novel_repo

    async def execute(self, novel_id: int, format: str = "txt") -> tuple[Path, str]:
        """Return ``(file_path, media_type)`` for the given novel.

        Raises:
            NotFoundError: If the novel doesn't exist, has no path, or the
                requested file doesn't exist on disk.
        """
        novel = await self._repo.get_by_id(novel_id)
        if not novel:
            raise NotFoundError(f"Novel {novel_id} not found")
        path = novel.get("path")
        if not path:
            raise NotFoundError(f"Novel {novel_id} has no file path")

        file_path = Path(path).with_suffix("." + format)
        if not file_path.is_file():
            raise NotFoundError(f"File not found for novel {novel_id}")

        media_type = "text/plain" if format == "txt" else "application/epub+zip"
        return file_path, media_type
