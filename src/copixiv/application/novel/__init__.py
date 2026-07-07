"""Application layer — novel use cases."""

from copixiv.application.novel.batch_download import BatchDownloadUseCase, BatchDownloadRequest
from copixiv.application.novel.count_novels import CountNovelsUseCase
from copixiv.application.novel.delete_novel import DeleteNovelUseCase
from copixiv.application.novel.download_novel import DownloadNovelUseCase
from copixiv.application.novel.get_novel_file import GetNovelFileUseCase
from copixiv.application.novel.list_novels import ListNovelsUseCase, ListNovelsRequest
from copixiv.application.novel.toggle_favourite import ToggleFavouriteUseCase
from copixiv.application.novel.toggle_special_follow import ToggleSpecialFollowUseCase

__all__ = [
    "BatchDownloadRequest",
    "BatchDownloadUseCase",
    "CountNovelsUseCase",
    "DeleteNovelUseCase",
    "DownloadNovelUseCase",
    "GetNovelFileUseCase",
    "ListNovelsRequest",
    "ListNovelsUseCase",
    "ToggleFavouriteUseCase",
    "ToggleSpecialFollowUseCase",
]
