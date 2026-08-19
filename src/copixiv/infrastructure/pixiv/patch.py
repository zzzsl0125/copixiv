"""Pixivpy3 monkey patches — applied once at startup.

These patches make pixivpy3 tolerant to API schema drift by falling back
to ``ParsedJson`` when Pydantic validation fails.  They are applied
explicitly via ``apply()``, never as an import-time side effect.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from copixiv.domain.exceptions import NovelNotFoundError
from copixiv.log import logger
from pixivpy3 import AppPixivAPI
from pixivpy3.aapi import ParsedJson, _MODE, _FILTER, DateOrStr
from pixivpy3.api import BasePixivAPI
from pixivpy3.utils import PixivError

from .errors import PixivHttpError

_patches_applied: bool = False


def safe_patch(name: str) -> Callable:
    """Decorator that wraps a patch function in try/except with logging.

    Eliminates the copy-pasted try/except boilerplate that was previously
    duplicated in all four ``_patch_*`` functions.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> None:
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception(f"Failed to patch {name}")
        return wrapper
    return decorator


def apply() -> None:
    """Apply all pixivpy3 monkey patches. Safe to call multiple times."""
    global _patches_applied
    if _patches_applied:
        return
    _patch_requests_call()
    _patch_parse_result()
    _patch_load_result_and_model()
    _patch_webview_novel()
    _patch_novel_ranking()
    _patches_applied = True
    logger.info("All pixivpy3 monkey patches applied.")


# -----------------------------------------------------------------------
# Patch: requests_call — raise on non-2xx so status codes survive
# -----------------------------------------------------------------------

@safe_patch("requests_call")
def _patch_requests_call() -> None:
    _original = BasePixivAPI.requests_call

    def _status_checked(self, method, url, headers=None, params=None, data=None, stream=False):
        response = _original(
            self, method, url, headers=headers, params=params, data=data, stream=stream
        )
        if response.status_code not in (200, 301, 302):
            raise PixivHttpError(
                f"HTTP {response.status_code} for {method} {url}",
                status_code=response.status_code,
                header=response.headers,
                body=response.text,
            )
        return response

    BasePixivAPI.requests_call = _status_checked


# -----------------------------------------------------------------------
# Patch: parse_result fallback
# -----------------------------------------------------------------------

@safe_patch("parse_result")
def _patch_parse_result() -> None:
    _original = AppPixivAPI.parse_result

    def _permissive(self, req):
        try:
            return _original(self, req)
        except Exception as e:
            logger.debug(
                f"Pydantic validation failed, falling back to ParsedJson: {e}"
            )
            try:
                return ParsedJson(json.loads(req.text))
            except Exception:
                return ParsedJson(
                    json.loads(req.content.decode("utf-8", "ignore"))
                )

    AppPixivAPI.parse_result = _permissive


# -----------------------------------------------------------------------
# Patch: webview_novel error handling
# -----------------------------------------------------------------------

@safe_patch("webview_novel")
def _patch_webview_novel() -> None:
    _original = AppPixivAPI.webview_novel

    def _patched(self, *args, **kwargs):
        try:
            return _original(self, *args, **kwargs)
        except PixivHttpError as e:
            # 404 = the novel is gone.  Other HTTP errors (429, 5xx, ...)
            # stay retryable — they bubble up to the client's retry loop.
            if e.status_code == 404:
                novel_id = args[0] if args else kwargs.get("novel_id")
                raise NovelNotFoundError(
                    f"novel #{novel_id} not found (HTTP 404)"
                ) from e
            raise
        except PixivError as e:
            # Extraction failure (missing/deleted content in the page) is
            # deterministic — the novel is not fetchable, don't retry.
            if "extract novel content" in str(e).lower():
                novel_id = args[0] if args else kwargs.get("novel_id")
                logger.error(f"Failed to fetch novel#{novel_id}: {e}")
                raise NovelNotFoundError(
                    f"novel #{novel_id} content extraction failed"
                ) from e
            raise

    AppPixivAPI.webview_novel = _patched


# -----------------------------------------------------------------------
# Patch: tolerant model loading
# -----------------------------------------------------------------------

def _sanitize_none_to_str(data: Any) -> Any:
    """Replace None → '' recursively so strict ``str`` fields accept data."""
    if isinstance(data, dict):
        return {k: _sanitize_none_to_str(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_sanitize_none_to_str(v) for v in data]
    return "" if data is None else data


def _permissive_model_construct(json_data: Any, model: type) -> Any:
    """Try model_validate, retry with None→'' sanitise, then fall back."""
    if isinstance(json_data, dict) and "error" in json_data:
        error_data = json_data["error"]
        if isinstance(error_data, dict):
            error_msg = error_data.get("message", str(error_data))
        else:
            error_msg = str(error_data)
        logger.warning(f"API error for {model.__name__}: {error_msg}")
        return json_data

    try:
        return model.model_validate(json_data)
    except Exception:
        try:
            sanitized = _sanitize_none_to_str(json_data)
            return model.model_validate(sanitized)
        except Exception:
            logger.warning(
                f"model_validate failed for {model.__name__}, "
                f"falling back to model_construct"
            )
            try:
                return model.model_construct(**json_data)
            except Exception:
                return json_data


@safe_patch("_load_result/_load_model")
def _patch_load_result_and_model() -> None:
    _original_load_result = AppPixivAPI._load_result
    _original_load_model = AppPixivAPI._load_model

    def _load_result(self, res, model):
        return _permissive_model_construct(self.parse_result(res), model)

    def _load_model(cls, data, model):
        return _permissive_model_construct(data, model)

    AppPixivAPI._load_result = _load_result
    AppPixivAPI._load_model = _load_model


# -----------------------------------------------------------------------
# Patch: novel_ranking (missing from pixivpy3)
# -----------------------------------------------------------------------

@safe_patch("novel_ranking")
def _patch_novel_ranking() -> None:

    def novel_ranking(
        self,
        mode: _MODE = "day_r18",
        filter: _FILTER = "for_ios",
        date: DateOrStr | None = None,
        offset: int | str | None = None,
        req_auth: bool = True,
    ) -> ParsedJson:
        url = f"{self.hosts}/v1/novel/ranking"
        params: dict[str, Any] = {"mode": mode, "filter": filter}
        if date:
            params["date"] = self._format_date(date)
        if offset:
            params["offset"] = offset
        r = self.no_auth_requests_call(
            "GET", url, params=params, req_auth=req_auth
        )
        return self.parse_result(r)

    AppPixivAPI.novel_ranking = novel_ranking
