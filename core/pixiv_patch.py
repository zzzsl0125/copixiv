from typing import Any
import json

from pixivpy3 import AppPixivAPI
from pixivpy3.aapi import ParsedJson
from pixivpy3.utils import PixivError

from core.db.database import db
from core.logger import logger

# --- Universal Monkey Patch for Pydantic Validation Errors ---
# Instead of subclassing models like WebviewNovel or Profile to loosen their types,
# we intercept the response parsing. If strict Pydantic validation fails, 
# we gracefully fallback to returning the generic ParsedJson wrapper which natively 
# supports dot-notation access identical to the models, avoiding crashes entirely.
try:
    _original_parse_result = AppPixivAPI.parse_result
    def permissive_parse_result(self, req):
        try:
            return _original_parse_result(self, req)
        except Exception as e:
            logger.debug(f"Pydantic validation failed, falling back to ParsedJson: {e}")
            try:
                return ParsedJson(json.loads(req.text))
            except Exception:
                return ParsedJson(json.loads(req.content.decode("utf-8", "ignore")))

    AppPixivAPI.parse_result = permissive_parse_result
    logger.info("Applied universal permissive monkey patch for AppPixivAPI.parse_result")
except Exception as e:
    logger.error(f"Failed to apply universal monkey patch for parse_result: {e}")

# --- Monkey Patch for AppPixivAPI.webview_novel ---
try:
    _original_webview_novel = AppPixivAPI.webview_novel

    def patched_webview_novel(self, *args, **kwargs):
        try:
            return _original_webview_novel(self, *args, **kwargs)
        except PixivError as e:
            if "extract novel content" in str(e).lower():
                novel_id = args[0] if args else kwargs.get("novel_id")
                logger.error(f"Failed to fetch novel#{novel_id}: {e}")
                with db.get_session(True) as session:
                    db.system(session).record_failure(novel_id, "fetch_content_error", str(e))
                return None
            raise

    AppPixivAPI.webview_novel = patched_webview_novel
    logger.info("Applied monkey patch for AppPixivAPI.webview_novel")
except Exception as e:
    logger.error(f"Failed to apply monkey patch for AppPixivAPI.webview_novel: {e}")

# --- Monkey Patch for AppPixivAPI.novel_ranking ---
try:
    from pixivpy3 import AppPixivAPI
    from pixivpy3.aapi import _MODE, _FILTER, DateOrStr, ParsedJson
    
    def novel_ranking(
        self,
        mode: _MODE = "day_r18",
        filter: _FILTER = "for_ios",
        date: DateOrStr | None = None,
        offset: int | str | None = None,
        req_auth: bool = True,
    ) -> ParsedJson:
        url = f"{self.hosts}/v1/novel/ranking"
        params: dict[str, Any] = {
            "mode": mode,
            "filter": filter,
        }
        if date:
            params["date"] = self._format_date(date)
        if offset:
            params["offset"] = offset
        r = self.no_auth_requests_call("GET", url, params=params, req_auth=req_auth)
        return self.parse_result(r)

    AppPixivAPI.novel_ranking = novel_ranking
    logger.info("Applied monkey patch for AppPixivAPI.novel_ranking")
except Exception as e:
    logger.error(f"Failed to apply monkey patch for AppPixivAPI.novel_ranking: {e}")

