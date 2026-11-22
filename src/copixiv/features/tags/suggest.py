"""Tag alias suggestion query backed by the in-process Rust ``TagIndex``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from copixiv.db import delta as engine_delta
from copixiv.query_engine.port import NovelQueryEnginePort


def _require_engine(
    session: Session,
    query_engine: NovelQueryEnginePort | None = None,
) -> NovelQueryEnginePort:
    eng = query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
    if eng is None or not eng.ready():
        raise RuntimeError("Rust query_engine is not attached or not ready on this session")
    engine_delta.sync_engine_if_needed(session, eng)
    return eng


def suggest_tag_aliases(
    session: Session,
    *,
    limit: int = 5,
    offset: int = 0,
    target_tag: str | None = None,
    query_engine: NovelQueryEnginePort | None = None,
) -> dict:
    """Suggest tag alias groupings using the in-process Rust engine."""
    eng = _require_engine(session, query_engine)
    items_rs, next_cursor = eng.suggest_tag_aliases(
        limit=limit,
        offset=int(offset or 0),
        target_tag=target_tag,
    )
    items = [
        {
            "target": {
                "id": item.target.id,
                "name": item.target.name,
                "reference_count": item.target.reference_count,
            },
            "candidates": [
                {
                    "id": cand.id,
                    "name": cand.name,
                    "reference_count": cand.reference_count,
                }
                for cand in item.candidates
            ],
        }
        for item in items_rs
    ]
    return {
        "items": items,
        "next_cursor": next_cursor,
    }
