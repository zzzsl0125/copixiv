"""Novel data layer — read/write repos and unified facade."""

from .read_repo import (
    COUNT_CAP,
    NovelCount,
    SQLAlchemyNovelReadRepository,
    _novel_from_orm,
)
from .write_repo import (
    BATCH_METADATA_COLUMNS,
    SQLAlchemyNovelRepository,
    SQLAlchemyNovelWriteRepository,
)

__all__ = [
    "BATCH_METADATA_COLUMNS",
    "COUNT_CAP",
    "NovelCount",
    "SQLAlchemyNovelReadRepository",
    "SQLAlchemyNovelRepository",
    "SQLAlchemyNovelWriteRepository",
    "_novel_from_orm",
]
