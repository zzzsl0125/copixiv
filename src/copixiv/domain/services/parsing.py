"""General parsing utilities — pure functions."""

from typing import Any


def safe_get(data: Any, key: str, default: Any = None) -> Any:
    """Read a field from a Pydantic model or dict without throwing.

    Returns *default* if *data* is ``None`` or the key is missing.
    """
    if data is None:
        return default
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def safe_set(data: Any, key: str, value: Any) -> None:
    """Set a field on a Pydantic model or dict."""
    if isinstance(data, dict):
        data[key] = value
    else:
        setattr(data, key, value)


def guess_series_order(navigation: Any) -> int | None:
    """Infer a novel's position in its series from Pixiv navigation data.

    Looks at ``prevNovel.contentOrder`` or ``nextNovel.contentOrder``.
    """
    try:
        if prev := safe_get(navigation, "prevNovel"):
            return int(safe_get(prev, "contentOrder")) + 1
        if nxt := safe_get(navigation, "nextNovel"):
            return int(safe_get(nxt, "contentOrder")) - 1
        return None
    except Exception:
        return None


def parse_search_keyword(keyword: str) -> dict[str, str]:
    """Parse a front-end search keyword string into a queries dict.

    Input format: ``"type:value;type:value;"`` (semicolon-delimited).

    Returns ``{value: type}``, e.g. ``{"R-18": "keyword", "12345": "author_id"}``.
    """
    queries: dict[str, str] = {}
    if not keyword or not keyword.strip():
        return queries

    for cond in keyword.replace("；", ";").split(";"):
        cond = cond.strip()
        if not cond:
            continue
        colon_idx = cond.find(":")
        if colon_idx > 0:
            qtype = cond[:colon_idx].strip()
            qvalue = cond[colon_idx + 1 :].strip()
            if qvalue:
                queries[qvalue] = qtype
        else:
            queries[cond] = "keyword"
    return queries
