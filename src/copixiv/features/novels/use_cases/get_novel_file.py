"""Use case: retrieve a novel's file path for download."""

from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.exceptions import NotFoundError
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


def get_novel_file(
    novel_id: int,
    fmt: str = "txt",
    *,
    novel_repo: SQLAlchemyNovelRepository,
    download_root: str = "download",
) -> tuple[Path, str]:
    """Return ``(file_path, media_type)`` for the given novel.

    The stored ``path`` is trusted only after confirming it resolves
    inside the download root — a polluted DB row must not turn this
    endpoint into an arbitrary-file read.

    Synchronous: one indexed row lookup plus a few ``Path`` checks.  The
    endpoint is a plain ``def`` so FastAPI keeps all of it (session
    included) in the worker thread.

    Raises:
        NotFoundError: If the novel doesn't exist, has no path, or the
            requested file doesn't exist on disk (or escapes the root).
    """
    novel = novel_repo.get_by_id(novel_id)
    if not novel:
        raise NotFoundError(f"Novel {novel_id} not found")
    path = novel.path
    if not path:
        raise NotFoundError(f"Novel {novel_id} has no file path")

    root = Path(download_root).resolve()
    file_path = formats.sibling_path(Path(path), fmt)
    try:
        resolved = file_path.resolve()
    except OSError as exc:
        raise NotFoundError(f"File not found for novel {novel_id}") from exc
    if not resolved.is_relative_to(root):
        raise NotFoundError(f"Novel {novel_id} path escapes the download directory")
    if not resolved.is_file():
        raise NotFoundError(f"File not found for novel {novel_id}")

    return resolved, formats.media_type_for(fmt)
