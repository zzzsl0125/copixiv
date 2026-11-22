"""In-process Rust query engine leaf adapter package."""

from .adapter import RustNovelQueryEngine
from .port import (
    EngineCountResult,
    EngineCursor,
    EnginePageRowsResult,
    EngineQuery,
)
from .sync import attach_and_start_engine, build_engine_from_store

__all__ = [
    "EngineCountResult",
    "EngineCursor",
    "EnginePageRowsResult",
    "EngineQuery",
    "RustNovelQueryEngine",
    "attach_and_start_engine",
    "build_engine_from_store",
]
