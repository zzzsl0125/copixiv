"""In-process Rust query engine leaf adapter package."""

from .adapter import RustNovelQueryEngine
from .port import (
    EngineCountResult,
    EngineCursor,
    EngineNovelRecord,
    EnginePageRowsResult,
    EngineQuery,
    NovelQueryEnginePort,
    PendingDeltaProtocol,
    SnapshotDeltaProtocol,
    SnapshotStoreProtocol,
    TagCandidateProtocol,
    TagSuggestItemProtocol,
)
from .sync import attach_and_start_engine, build_engine_from_store

__all__ = [
    "EngineCountResult",
    "EngineCursor",
    "EngineNovelRecord",
    "EnginePageRowsResult",
    "EngineQuery",
    "NovelQueryEnginePort",
    "PendingDeltaProtocol",
    "RustNovelQueryEngine",
    "SnapshotDeltaProtocol",
    "SnapshotStoreProtocol",
    "TagCandidateProtocol",
    "TagSuggestItemProtocol",
    "attach_and_start_engine",
    "build_engine_from_store",
]
