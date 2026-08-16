"""QuerySpec — the domain value object for novel list/count/scope queries.

Replaces the bare ``(conditions, order_by, order_direction, cursor,
per_page, min_like, min_text, exclude_ids, exclude_blocked_tags)``
parameter soup that used to travel from the web layer through the
repository port into the SQL query builder (docs/MODULARITY.md §M3).

The repository layer supplies the SQL-only inputs (blocked tag names,
restricted ID sets) separately when constructing the builder — they are
infrastructure concerns, not part of the user-facing query.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from copixiv.domain.services.parsing import SearchConditions


class QuerySpec(BaseModel):
    """A complete read-query specification for novels.

    Fields:
        conditions: Parsed search conditions (``parse_search_keyword``).
        order_by / order_direction: List ordering; ``random`` uses the
            precomputed shuffle column.
        cursor: Opaque keyset-pagination cursor (``{"id": ..., <order_by>: ...}``
            or ``{"shuffle": ..., "id": ...}`` for random browsing).
        per_page: Page size (+1 internally to detect more pages).
        min_like / min_text: Display thresholds (0 disables).
        exclude_ids: Novel IDs to drop from results (scope complements).
        exclude_blocked_tags: Override for the blocked-tag exclusion
            policy (``None`` → global runtime setting, see
            ``domain/services/exclusion.py``).
    """

    conditions: SearchConditions = Field(default_factory=list)
    order_by: str = "like"
    order_direction: str = "DESC"
    cursor: dict[str, Any] | None = None
    per_page: int = 50
    min_like: int | None = None
    min_text: int | None = None
    exclude_ids: list[int] = Field(default_factory=list)
    exclude_blocked_tags: bool | None = None
