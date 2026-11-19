"""Composite tag repository facade preserving backward-compatible access."""

from __future__ import annotations

from sqlalchemy.orm import Session

from copixiv.db import delta as engine_delta
from copixiv.query_engine.port import NovelQueryEnginePort

from .alias_repo import TagAliasRepository
from .apply_alias import apply_alias_retroactively
from .preference_repo import TagPreferenceRepository
from .suggest import suggest_tag_aliases
from .tag_repo import TagRepository


class SQLAlchemyTagRepository(
    TagAliasRepository,
    TagPreferenceRepository,
    TagRepository,
):
    """Facade combining tag, preference, alias, and suggestion operations."""

    def __init__(
        self,
        session: Session,
        *,
        query_engine: NovelQueryEnginePort | None = None,
    ):
        super().__init__(session)
        self._query_engine: NovelQueryEnginePort | None = (
            query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
        )

    def suggest_aliases(
        self,
        limit: int = 5,
        offset: int = 0,
        target_tag: str | None = None,
    ) -> dict:
        """Suggest alias mappings using the in-process Rust ``TagIndex``."""
        return suggest_tag_aliases(
            self.session,
            limit=limit,
            offset=offset,
            target_tag=target_tag,
            query_engine=self._query_engine,
        )

    def apply_alias_retroactively(self, source: str, target: str) -> int:
        """Replace all occurrences of *source* tag with *target* tag on novels."""
        return apply_alias_retroactively(self.session, source, target)
