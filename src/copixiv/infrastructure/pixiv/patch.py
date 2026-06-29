"""Pixivpy3 monkey patches — applied once at startup.

These patches make pixivpy3 tolerant to API schema drift by falling back
to ``ParsedJson`` when Pydantic validation fails.  They are applied
explicitly via ``apply()``, never as an import-time side effect.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pixivpy3 import AppPixivAPI
from pixivpy3.aapi import ParsedJson, _MODE, _FILTER, DateOrStr
from pixivpy3.utils import PixivError

logger = logging.getLogger("copixiv")

_patches_applied: bool = False


def apply() -> None:
    """Apply all pixivpy3 monkey patches. Safe to call multiple times."""
    global _patches_applied
    if _patches_applied:
        return
    _patch_parse_result()
    _patch_load_result_and_model()
    _patch_webview_novel()
    _patch_novel_ranking()
    _patches_applied = True
    logger.info("All pixivpy3 monkey patches applied.")


# -----------------------------------------------------------------------
# Patch: parse_result fallback
# -----------------------------------------------------------------------

def _patch_parse_result() -> None:
    try:
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
    except Exception:
        logger.exception("Failed to patch parse_result")


# -----------------------------------------------------------------------
# Patch: webview_novel error handling
# -----------------------------------------------------------------------

def _patch_webview_novel() -> None:
    try:
        _original = AppPixivAPI.webview_novel

        def _patched(self, *args, **kwargs):
            try:
                return _original(self, *args, **kwargs)
            except PixivError as e:
                if "extract novel content" in str(e).lower():
                    novel_id = args[0] if args else kwargs.get("novel_id")
                    logger.error(f"Failed to fetch novel#{novel_id}: {e}")
                    return None
                raise

        AppPixivAPI.webview_novel = _patched
    except Exception:
        logger.exception("Failed to patch webview_novel")


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
        error_msg = json_data["error"].get("message", str(json_data["error"]))
        logger.error(f"API error for {model.__name__}: {error_msg}")
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


def _patch_load_result_and_model() -> None:
    try:
        _original_load_result = AppPixivAPI._load_result
        _original_load_model = AppPixivAPI._load_model

        def _load_result(self, res, model):
            return _permissive_model_construct(self.parse_result(res), model)

        def _load_model(cls, data, model):
            return _permissive_model_construct(data, model)

        AppPixivAPI._load_result = _load_result
        AppPixivAPI._load_model = _load_model
    except Exception:
        logger.exception("Failed to patch _load_result/_load_model")


# -----------------------------------------------------------------------
# Patch: novel_ranking (missing from pixivpy3)
# -----------------------------------------------------------------------

def _patch_novel_ranking() -> None:
    try:

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
    except Exception:
        logger.exception("Failed to patch novel_ranking")
