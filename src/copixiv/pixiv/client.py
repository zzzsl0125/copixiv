"""PixivClient — typed async API endpoints with tenacity retry and pagination."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from copixiv.core.exceptions import NovelNotFoundError
from copixiv.log import logger

from .account import (
    PIXIV_APP_HOSTS,
    AccountInvalidError,
    AccountStrategy,
    PixivAccount,
    PixivApiError,
    RateLimitError,
)
from .accounts import AccountPool
from .models import (
    NovelListResponse,
    PixivNovelInfo,
    UserDetailResponse,
    WebviewNovel,
)

_WEBVIEW_NOVEL_RE = re.compile(r"novel:\s({.+}),\s+isOwnWork")
_ALLOWED_NEXT_HOST = "app-api.pixiv.net"

# 4xx statuses that *are* worth retrying: 401 (expired/revoked access token),
# 408 (request timeout), and 429 (rate limited — handled via cooldown).
_TRANSIENT_4XX: frozenset[int] = frozenset({401, 408, 429})


def _format_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _is_permanent(exc: PixivApiError) -> bool:
    """True when retrying *exc* cannot change the outcome."""
    status = exc.status_code
    if status is None:
        return False
    return 400 <= status < 500 and status not in _TRANSIENT_4XX


def _is_retryable_exc(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (AccountInvalidError, RateLimitError, httpx.HTTPError, ConnectionError),
    ):
        return True
    if isinstance(exc, PixivApiError):
        return not _is_permanent(exc)
    return False


def _extract_webview_payload(novel_id: int, resp: httpx.Response) -> dict[str, Any]:
    match = _WEBVIEW_NOVEL_RE.search(resp.text)
    if not match:
        logger.error(f"Failed to fetch novel#{novel_id}: extract novel content failed")
        raise NovelNotFoundError(f"novel #{novel_id} content extraction failed")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        logger.error(f"Failed to parse novel#{novel_id} JSON: {exc}")
        raise NovelNotFoundError(f"novel #{novel_id} content extraction failed") from exc


class PixivClient:
    """Pixiv API client with typed endpoints, account pooling, and pagination."""

    def __init__(
        self,
        account_pool: AccountPool,
        max_concurrency: int = 5,
    ):
        self.pool = account_pool
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def account_rule(
        self,
        need_premium: bool = False,
        force_follow: bool = False,
    ):
        """Temporarily override account selection strategy."""
        token = self.pool.set_strategy(
            AccountStrategy(
                need_premium=need_premium,
                force_follow=force_follow,
            )
        )
        try:
            yield self
        finally:
            self.pool.reset_strategy(token)

    # ---- Public API methods ------------------------------------------------

    async def webview_novel(self, novel_id: int) -> WebviewNovel:
        """Fetch a single novel with full text content."""
        try:
            payload = await self._request_with_retry(
                "GET",
                f"{PIXIV_APP_HOSTS}/webview/v2/novel",
                label=f"webview_novel({novel_id})",
                params={"id": novel_id, "viewer_version": "20221031_ai"},
                extract_payload=lambda resp: _extract_webview_payload(novel_id, resp),
            )
        except PixivApiError as exc:
            if exc.status_code == 404:
                raise NovelNotFoundError(f"novel #{novel_id} not found (HTTP 404)") from exc
            raise
        return WebviewNovel.model_validate(payload)

    async def user_novels(
        self,
        author_id: int,
        fetch_all: bool = False,
    ) -> NovelListResponse:
        """Fetch all novels by an author."""
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v1/user/novels",
            label=f"user_novels({author_id})",
            params={"user_id": author_id, "filter": "for_ios"},
        )
        result = NovelListResponse.model_validate(payload)
        if fetch_all:
            return await self._paginate("user_novels", result, None, None)
        return result

    async def user_detail(self, user_id: int) -> UserDetailResponse:
        """Fetch user profile details."""
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v1/user/detail",
            label=f"user_detail({user_id})",
            params={"user_id": user_id, "filter": "for_ios"},
        )
        return UserDetailResponse.model_validate(payload)

    async def user_follow_add(self, user_id: int) -> dict[str, Any]:
        """Follow a user."""
        return await self._request_with_retry(
            "POST",
            f"{PIXIV_APP_HOSTS}/v1/user/follow/add",
            label=f"user_follow_add({user_id})",
            data={"user_id": user_id, "restrict": "public"},
        )

    async def user_follow_delete(self, user_id: int) -> dict[str, Any]:
        """Unfollow a user."""
        return await self._request_with_retry(
            "POST",
            f"{PIXIV_APP_HOSTS}/v1/user/follow/delete",
            label=f"user_follow_delete({user_id})",
            data={"user_id": user_id},
        )

    async def novel_follow(self, fetch_til: datetime | None = None) -> NovelListResponse:
        """Fetch novels from followed users."""
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v1/novel/follow",
            label="novel_follow()",
            params={"restrict": "public"},
        )
        result = NovelListResponse.model_validate(payload)
        if fetch_til is not None:
            return await self._paginate("novel_follow", result, fetch_til, None)
        return result

    async def novel_ranking(
        self,
        mode: str = "day_r18",
        date: Any = None,
        fetch_all: bool = False,
    ) -> NovelListResponse:
        """Fetch novel rankings."""
        params: dict[str, Any] = {"mode": mode, "filter": "for_ios"}
        if date:
            params["date"] = _format_date(date)
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v1/novel/ranking",
            label=f"novel_ranking(mode={mode!r})",
            params=params,
        )
        result = NovelListResponse.model_validate(payload)
        if fetch_all:
            return await self._paginate("novel_ranking", result, None, None)
        return result

    async def search_novel(
        self,
        keyword: str,
        search_type: str = "keyword",
        order: str = "popular_desc",
        start_date: Any = None,
        end_date: Any = None,
        fetch_minlike: int | None = None,
    ) -> NovelListResponse:
        """Search novels by keyword."""
        params: dict[str, Any] = {
            "word": keyword,
            "search_target": search_type or "partial_match_for_tags",
            "sort": order or "date_desc",
            "merge_plain_keyword_results": "true",
            "include_translated_tag_results": "true",
        }
        if start_date:
            params["start_date"] = _format_date(start_date)
        if end_date:
            params["end_date"] = _format_date(end_date)
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v1/search/novel",
            label=f"search_novel({keyword!r})",
            params=params,
        )
        result = NovelListResponse.model_validate(payload)
        if fetch_minlike is not None:
            return await self._paginate("search_novel", result, None, fetch_minlike)
        return result

    async def novel_series(
        self,
        series_id: int,
        fetch_all: bool = False,
    ) -> NovelListResponse:
        """Fetch all novels in a series."""
        payload = await self._request_with_retry(
            "GET",
            f"{PIXIV_APP_HOSTS}/v2/novel/series",
            label=f"novel_series({series_id})",
            params={"series_id": series_id, "filter": "for_ios"},
        )
        result = NovelListResponse.model_validate(payload)
        if fetch_all:
            return await self._paginate("novel_series", result, None, None)
        return result

    # ---- Internal call machinery -------------------------------------------

    _MAX_RETRIES: int = 3
    _BACKOFF_BASE: float = 1.0  # seconds — doubled each attempt
    _BACKOFF_CAP: float = 10.0  # seconds
    _MAX_PAGES: int = 200

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff: 1s, 2s, 4s, capped at 10s."""
        return min(
            PixivClient._BACKOFF_BASE * (2**attempt),
            PixivClient._BACKOFF_CAP,
        )

    def _wait_seconds(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, AccountInvalidError):
            return 0.0
        if isinstance(exc, RateLimitError):
            if self.pool.has_ready_account():
                return 0.0
            if exc.retry_after is not None:
                return min(max(exc.retry_after, 0.0), self._BACKOFF_CAP)
        attempt = max(retry_state.attempt_number - 1, 0)
        return self._backoff(attempt)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        label: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extract_payload: Callable[[httpx.Response], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Call ``account.request()`` with ``tenacity`` backoff and account rotation."""
        total_attempts = self._MAX_RETRIES + 1
        current_account: list[PixivAccount | None] = [None]

        def _log_before_sleep(retry_state: RetryCallState) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            account = current_account[0]
            attempt_no = retry_state.attempt_number
            if isinstance(exc, AccountInvalidError):
                logger.warning(
                    f"Account {account} is invalid, switching (attempt {attempt_no}/{total_attempts})"
                )
            elif isinstance(exc, RateLimitError):
                logger.warning(f"Rate limited on {account}, retry {attempt_no}/{total_attempts}")
            else:
                logger.warning(f"API error on {account}: {exc}, retry {attempt_no}/{total_attempts}")

        async def _async_sleep(seconds: float) -> None:
            if seconds > 0:
                await asyncio.sleep(seconds)

        retrying = AsyncRetrying(
            stop=stop_after_attempt(total_attempts),
            retry=retry_if_exception(_is_retryable_exc),
            wait=self._wait_seconds,
            before_sleep=_log_before_sleep,
            sleep=_async_sleep,
            reraise=True,
        )

        async for attempt in retrying:
            with attempt:
                account = self.pool.select()
                current_account[0] = account
                logger.info(f"{account} API → {label}")
                try:
                    async with self._semaphore:
                        result = await account.request(
                            method,
                            url,
                            params=params,
                            data=data,
                            extract_payload=extract_payload,
                        )
                    logger.debug(f"{account} {label} completed.")
                    return result
                except PixivApiError as exc:
                    if _is_permanent(exc):
                        logger.warning(
                            f"Permanent API error on {account} ({exc.status_code}): {exc} — not retrying"
                        )
                    raise

        raise AssertionError("unreachable")

    async def _fetch_next_page(self, next_url: str) -> NovelListResponse:
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.netloc != _ALLOWED_NEXT_HOST:
            raise PixivApiError(
                f"Refusing unexpected next_url origin: {next_url}",
                status_code=400,
            )
        payload = await self._request_with_retry(
            "GET",
            next_url,
            label="get_next_page",
        )
        return NovelListResponse.model_validate(payload)

    async def _paginate(
        self,
        method: str,
        result: NovelListResponse,
        fetch_til: datetime | None,
        fetch_minlike: int | None,
    ) -> NovelListResponse:
        """Follow ``next_url`` across pages, collecting results."""
        page = 1

        novels_count = len(result.novels)
        logger.info(f"Paginate {method}: page {page} — {novels_count} items")

        next_url = result.next_url
        while next_url:
            page += 1
            if page > self._MAX_PAGES:
                logger.warning(
                    f"Paginate {method}: exceeded {self._MAX_PAGES} pages, stopping",
                )
                break

            next_result = await self._fetch_next_page(next_url)
            next_novels = next_result.novels
            new_next_url = next_result.next_url

            result.novels.extend(next_novels)
            result.next_url = new_next_url

            novels_count += len(next_novels)
            logger.info(
                f"Paginate {method}: page {page} — {len(next_novels)} items (total: {novels_count})",
            )

            next_url = new_next_url

            if next_novels and self._should_stop(next_novels[-1], fetch_til, fetch_minlike):
                logger.info(
                    f"Paginate {method}: stopping — threshold reached",
                )
                break

        logger.info(
            f"Paginate {method}: done — {page} pages, {novels_count} items total",
        )
        return result

    @staticmethod
    def _should_stop(
        last_item: PixivNovelInfo,
        fetch_til: datetime | None,
        fetch_minlike: int | None,
    ) -> bool:
        """Determine if pagination should stop based on date or bookmark thresholds."""
        if fetch_til:
            raw_date = last_item.create_date
            if raw_date:
                try:
                    create_dt = datetime.fromisoformat(raw_date.strip())
                    if create_dt.tzinfo is not None and fetch_til.tzinfo is None:
                        create_dt = create_dt.replace(tzinfo=None)
                    elif create_dt.tzinfo is None and fetch_til.tzinfo is not None:
                        fetch_til = fetch_til.replace(tzinfo=None)
                    if create_dt < fetch_til:
                        return True
                except Exception:
                    return False
        return bool(fetch_minlike and last_item.total_bookmarks < fetch_minlike)
