"""Domain ports — Protocols that infrastructure must implement."""

from .repositories import (
    NovelRepository,
    AuthorRepository,
    SeriesRepository,
    TagRepository,
    TokenRepository,
    TaskRepository,
    SearchHistoryRepository,
)
from .pixiv import PixivNovelPort, PixivAccountPort
from .storage import FileStoragePort, ImageDownloaderPort
from .epub import EpubBuilderPort
from .unit_of_work import UnitOfWork
from .notifier import NotifierPort

__all__ = [
    "NovelRepository",
    "AuthorRepository",
    "SeriesRepository",
    "TagRepository",
    "TokenRepository",
    "TaskRepository",
    "SearchHistoryRepository",
    "PixivNovelPort",
    "PixivAccountPort",
    "FileStoragePort",
    "ImageDownloaderPort",
    "EpubBuilderPort",
    "UnitOfWork",
    "NotifierPort",
]
