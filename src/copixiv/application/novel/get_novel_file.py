"""Use case: retrieve a novel's file path for download."""

from pathlib import Path

from sqlalchemy.orm import Session

from copixiv.domain.exceptions import NotFoundError
from copixiv.infrastructure.database import models


class GetNovelFileUseCase:
    """Look up a novel and verify its file exists on disk.

    Returns the resolved file path and media type so the HTTP layer can
    serve a ``FileResponse``.
    """

    def __init__(self, db: Session):
        self._db = db

    def execute(self, novel_id: int, format: str = "txt") -> tuple[Path, str]:
        """Return ``(file_path, media_type)`` for the given novel.

        Raises:
            NotFoundError: If the novel doesn't exist, has no path, or the
                requested file doesn't exist on disk.
        """
        novel = self._db.get(models.Novel, novel_id)
        if not novel:
            raise NotFoundError(f"Novel {novel_id} not found")
        if not novel.path:
            raise NotFoundError(f"Novel {novel_id} has no file path")

        file_path = Path(novel.path).with_suffix("." + format)
        if not file_path.is_file():
            raise NotFoundError(f"File not found for novel {novel_id}")

        media_type = "text/plain" if format == "txt" else "application/epub+zip"
        return file_path, media_type
