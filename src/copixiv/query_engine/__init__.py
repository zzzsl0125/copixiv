"""In-process Rust query engine leaf adapter package."""

from .adapter import RustNovelQueryEngine
from .port import (
    EngineCountResult,
    EngineCursor,
    EngineMode,
    EnginePageResult,
    EngineQuery,
    NovelQueryEnginePort,
    PendingDeltaProtocol,
    SnapshotStoreProtocol,
)
from .sync import attach_and_start_engine, build_engine_from_store, rebuild_engine

__all__ = [
    "EngineCountResult",
    "EngineCursor",
    "EngineMode",
    "EnginePageResult",
    "EngineQuery",
    "NovelQueryEnginePort",
    "PendingDeltaProtocol",
    "RustNovelQueryEngine",
    "SnapshotStoreProtocol",
    "attach_and_start_engine",
    "build_engine_from_store",
    "rebuild_engine",
]
