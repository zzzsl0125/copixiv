"""General parsing utilities — pure functions."""

import re
from typing import Any


def safe_get(data: Any, path: str, default: Any = None) -> Any:
    """Read a (possibly nested) field from a Pydantic model or dict.

    Supports dotted paths: ``safe_get(novel, "user.name")`` is equivalent to
    ``safe_get(safe_get(novel, "user"), "name")``.

    Returns *default* if *data* is ``None``, any intermediate key is missing,
    or the final key is missing.  A terminal ``None`` value is NOT treated as
    missing — if the final field exists and is ``None``, ``None`` is returned.
    """
    if data is None:
        return default
    keys = path.split(".")
    for i, key in enumerate(keys):
        if data is None:
            return default
        is_last = i == len(keys) - 1
        if isinstance(data, dict):
            if is_last:
                return data.get(key, default)
            data = data.get(key)
        else:
            if is_last:
                return getattr(data, key, default)
            data = getattr(data, key, None)
    return default


def safe_set(data: Any, path: str, value: Any) -> None:
    """Set a (possibly nested) field on a Pydantic model or dict.

    Dotted paths traverse through intermediate keys, creating missing dicts
    along the way.  For Pydantic model attributes, intermediate attributes
    must already exist.
    """
    if data is None:
        raise ValueError("Cannot set on None")
    keys = path.split(".")
    for key in keys[:-1]:
        if isinstance(data, dict):
            if key not in data:
                data[key] = {}
            data = data[key]
        else:
            data = getattr(data, key)
    last = keys[-1]
    if isinstance(data, dict):
        data[last] = value
    else:
        setattr(data, last, value)


def guess_series_order(navigation: Any) -> int | None:
    """Infer a novel's position in its series from Pixiv navigation data.

    Looks at ``prevNovel.contentOrder`` first (current = prev + 1), and
    falls back to ``nextNovel.contentOrder`` (current = next - 1) when the
    previous pointer exists but carries no order.
    """
    try:
        prev = safe_get(navigation, "prevNovel")
        if prev is not None:
            order = safe_get(prev, "contentOrder")
            if order is not None:
                return int(order) + 1
        nxt = safe_get(navigation, "nextNovel")
        if nxt is not None:
            order = safe_get(nxt, "contentOrder")
            if order is not None:
                return int(order) - 1
        return None
    except (TypeError, ValueError):
        return None


# An ordered search condition: (type, value).  The canonical wire format
# is the raw keyword string ``"type:value;type:value"`` — this parser is
# its single, authoritative translation into typed conditions.
SearchConditions = list[tuple[str, str]]

# Bare digit-only tokens of 7+ characters are treated as novel IDs (the
# smallest ID in the dataset is 1,285,180 — shorter numbers can never be
# a valid ID; use the explicit ``id:123`` form to force ID lookup).
_BARE_ID_RE = re.compile(r"^\d{7,}$")


def parse_search_keyword(keyword: str) -> SearchConditions:
    """Parse a search keyword string into an ordered list of conditions.

    Input format: ``"type:value;type:value;"`` (semicolon-delimited,
    Chinese full-width ``；`` accepted).  Each condition becomes a
    ``(type, value)`` pair, e.g. ``"keyword:R-18;author_id:12345"`` →
    ``[("keyword", "R-18"), ("author_id", "12345")]``.

    Semantics (the contract the whole search path relies on):

    - ALL conditions combine with AND — every additional condition
      narrows the result set.  Multi-valued facets (``tags``, ``keyword``)
      each contribute one AND-branch per value, which is satisfiable
      because a novel holds many tags / an FTS row matches many tokens.
    - Single-valued columns (``author_id`` / ``series_id`` / ``id`` /
      ``is_favourite`` / ``is_special_follow``) keep the LAST value when a
      type repeats — under AND semantics two different values for the
      same column are contradictory, and order is preserved so the winner
      is deterministic.
    - Unknown types are kept in the list and rejected downstream with a
      400 (``Invalid query field``) — a typo must be loud, not a silent
      empty result set.
    - The list preserves duplicates and order, unlike the old lossy
      ``{value: type}`` dict (which dropped conditions whose values
      collided).
    """
    conditions: SearchConditions = []
    if not keyword or not keyword.strip():
        return conditions

    for cond in keyword.replace("；", ";").split(";"):
        cond = cond.strip()
        if not cond:
            continue
        colon_idx = cond.find(":")
        if colon_idx > 0:
            qtype = cond[:colon_idx].strip()
            qvalue = cond[colon_idx + 1 :].strip()
            if qvalue:
                conditions.append((qtype, qvalue))
        else:
            qtype = "id" if _BARE_ID_RE.match(cond) else "keyword"
            conditions.append((qtype, cond))
    return conditions
