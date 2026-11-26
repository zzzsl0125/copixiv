"""Tag alias resolution helper for single-hop tag canonicalization."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from copixiv.core.text import parse_tags
from copixiv.features.tags.repo import SQLAlchemyTagRepository


class TagAliasResolver:
    """Resolve raw or parsed tags through the persisted ``tag_alias`` table."""

    def __init__(self, session: Session) -> None:
        self._alias_map: dict[str, str] = SQLAlchemyTagRepository(session).get_alias_map()

    @property
    def alias_map(self) -> dict[str, str]:
        """Return the loaded ``{source_tag: target_tag}`` mapping."""
        return self._alias_map

    def resolve(
        self,
        tags: Iterable[str | dict[str, Any]],
        *,
        sort: bool = True,
    ) -> list[str]:
        """Parse, alias-map, and deduplicate *tags*."""
        cleaned = parse_tags(list(tags))
        mapped = [self._alias_map.get(tag, tag) for tag in cleaned]
        if sort:
            return sorted(set(mapped))
        return list(dict.fromkeys(mapped))

    def resolve_set(self, tags: Iterable[str]) -> set[str]:
        """Map already-parsed *tags* through the alias table into a set."""
        return {self._alias_map.get(tag, tag) for tag in tags}


__all__ = ["TagAliasResolver"]
