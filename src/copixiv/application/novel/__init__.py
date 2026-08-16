"""Application layer — novel use cases (download, batch, delete, file)."""

from copixiv.application.novel.batch_download import BatchDownloadUseCase
from copixiv.application.novel.batch_operations import (
    BATCH_ID_CHUNK_SIZE,
    BATCH_MAX_NOVELS,
    BATCH_MAX_TAGS,
    BatchDeleteUseCase,
    BatchTagUseCase,
    resolve_batch_scope,
)
from copixiv.application.novel.delete_novel import DeleteNovelUseCase
from copixiv.application.novel.download_novel import DownloadNovelUseCase
from copixiv.application.novel.get_novel_file import GetNovelFileUseCase

__all__ = [
    "BATCH_ID_CHUNK_SIZE",
    "BATCH_MAX_NOVELS",
    "BATCH_MAX_TAGS",
    "BatchDownloadUseCase",
    "BatchDeleteUseCase",
    "BatchTagUseCase",
    "resolve_batch_scope",
    "DeleteNovelUseCase",
    "DownloadNovelUseCase",
    "GetNovelFileUseCase",
]
