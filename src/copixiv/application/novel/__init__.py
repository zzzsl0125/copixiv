"""Application layer — novel use cases (download, batch, delete, file)."""

from copixiv.application.novel.batch_download import BatchDownloadUseCase
from copixiv.application.novel.delete_novel import DeleteNovelUseCase
from copixiv.application.novel.download_novel import DownloadNovelUseCase
from copixiv.application.novel.get_novel_file import GetNovelFileUseCase

__all__ = [
    "BatchDownloadUseCase",
    "DeleteNovelUseCase",
    "DownloadNovelUseCase",
    "GetNovelFileUseCase",
]
