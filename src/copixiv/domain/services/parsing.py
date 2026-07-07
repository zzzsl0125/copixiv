"""General parsing utilities — pure functions."""

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
